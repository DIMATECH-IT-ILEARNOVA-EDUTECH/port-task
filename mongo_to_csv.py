#!/usr/bin/env python3
"""
MongoDB Collections Exporter and Mongo -> Postgres Incremental Sync

This script can:
- Export MongoDB collections to CSV (with optional flattening).
- Incrementally sync MongoDB collections into a Postgres database, creating/evolving
  tables and upserting rows based on an 'updated' timestamp field.

Usage:
    CSV Export:
      python mongo_to_csv.py users orders
      python mongo_to_csv.py products --output products.csv

    Postgres Sync:
      python mongo_to_csv.py users orders \
        --sync-postgres \
        --pg-url "postgres://user:pass@localhost:5432/warehouse" \
        --pg-schema analytics \
        --database mydb \
        --connection "mongodb://localhost:27017" \
        --batch-size 1000 \
        --index-fields "email,created_at"

Requirements:
    - pymongo
    - pandas
    - psycopg2-binary
    - python-dateutil
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
except ImportError:
    print("Error: pymongo is required. Install it with: pip install pymongo")
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("Error: pandas is required. Install it with: pip install pandas")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras as pg_extras
except ImportError:
    print("Error: psycopg2-binary is required. Install it with: pip install psycopg2-binary")
    sys.exit(1)

try:
    from dateutil import parser as dtparser
except ImportError:
    print("Error: python-dateutil is required. Install it with: pip install python-dateutil")
    sys.exit(1)


def setup_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    return logging.getLogger(__name__)


def sanitize_identifier(name: str, max_len: int = 63) -> str:
    """
    Sanitize a string to a valid Postgres identifier: lowercase, replace non-word chars with '_',
    trim to max length, and ensure it does not start with a digit.
    """
    s = name.lower()
    s = re.sub(r"[^\w]+", "_", s)
    s = s.strip("_")
    if not s:
        s = "col"
    if s[0].isdigit():
        s = f"_{s}"
    if len(s) > max_len:
        s = s[:max_len]
    return s


def quote_ident(ident: str) -> str:
    # Minimal identifier quoting to avoid reserved words issues
    return '"' + ident.replace('"', '""') + '"'


def to_utc_datetime(value: Any) -> Optional[datetime]:
    """
    Convert incoming value to an aware UTC datetime if possible.
    Accepts datetime (naive or aware), string timestamp, or Unix timestamp (int/float).
    """
    if value is None:
        return None

    # Handle datetime objects
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt

    # Handle Unix timestamps (numbers)
    if isinstance(value, (int, float)):
        try:
            if value > 1e10:
                timestamp = value / 1000.0
            else:
                timestamp = value
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            return dt
        except (ValueError, OSError):
            return None

    # Handle string timestamps
    if isinstance(value, str):
        try:
            try:
                timestamp = float(value)
                if timestamp > 1e10:
                    timestamp = timestamp / 1000.0
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                return dt
            except ValueError:
                pass

            # Fall back to dateutil parsing
            dt = dtparser.parse(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            return None

    return None


def infer_pg_type(value: Any) -> Optional[str]:
    """
    Infer a Postgres type name based on a Python value.
    Returns one of: jsonb, timestamptz, boolean, bigint, double precision, text, or None.
    """
    if value is None:
        return None
    # JSON-like
    if isinstance(value, (dict, list)):
        return "jsonb"
    # datetime - check datetime objects first
    if isinstance(value, datetime):
        return "timestamptz"
    # primitives (check bool before int since bool is subclass of int)
    if isinstance(value, bool):
        return "boolean"

    # Check for Unix timestamps (common pattern in MongoDB)
    if isinstance(value, (int, float)):
        if isinstance(value, float) or (isinstance(value, int) and value > 1000000000):
            dt = to_utc_datetime(value)
            if dt is not None:
                return "timestamptz"
        # Regular numbers
        if isinstance(value, int):
            return "bigint"
        else:
            return "double precision"

    # For strings, try to detect if it's a timestamp
    if isinstance(value, str):
        # Only try datetime parsing for strings that look like timestamps
        if any(char in value for char in ['-', ':', 'T', 'Z']) and len(value) > 8:
            dt = to_utc_datetime(value)
            if dt is not None:
                return "timestamptz"
    # default to text
    return "text"


def promote_type(t1: str, t2: str) -> str:
    """
    Promote two inferred types to a common type that can hold both values safely.
    Priority:
      jsonb dominates everything
      timestamptz conflicts -> text (unless equal)
      numeric promotion: bigint + double precision -> double precision
      boolean mixed with numeric/text -> text
      text mixed with anything -> text
    """
    if t1 == t2:
        return t1
    if "jsonb" in (t1, t2):
        return "jsonb"
    if "timestamptz" in (t1, t2):
        # If both timestamptz handled above; otherwise promote to text
        return "text"
    # numeric promotions
    numeric = {"bigint", "double precision"}
    if t1 in numeric and t2 in numeric:
        return "double precision"
    # any boolean mixed -> text
    if "boolean" in (t1, t2):
        return "text"
    # fallback to text
    return "text"


class MongoToCSVExporter:
    """Handles MongoDB to CSV export operations."""

    def __init__(self, connection_string: str = "mongodb://localhost:27017/",
                 database_name: str = "test", timeout: int = 5000, logger: Optional[logging.Logger] = None):
        self.connection_string = connection_string
        self.database_name = database_name
        self.timeout = timeout
        self.client = None
        self.db = None
        self.logger = logger or setup_logger()

    def connect(self) -> bool:
        try:
            self.client = MongoClient(
                self.connection_string,
                serverSelectionTimeoutMS=self.timeout
            )

            self.client.admin.command('ping')
            self.db = self.client[self.database_name]
            self.logger.info(f"Successfully connected to MongoDB database: {self.database_name}")
            return True
        except (ConnectionFailure, ServerSelectionTimeoutError) as e:
            self.logger.error(f"Failed to connect to MongoDB: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error connecting to MongoDB: {e}")
            return False

    def collection_exists(self, collection_name: str) -> bool:
        if self.db is None:
            self.logger.error("Database connection not established")
            return False
        collection_names = list(self.db.list_collection_names())
        exists = collection_name in collection_names
        if exists:
            self.logger.info(f"Collection '{collection_name}' found in database")
        else:
            self.logger.warning(f"Collection '{collection_name}' not found in database")
            self.logger.info(f"Available collections: {collection_names}")
        return exists

    def get_collection_info(self, collection_name: str) -> Dict[str, Any]:
        if self.db is None:
            return {}
        collection = self.db[collection_name]
        doc_count = collection.count_documents({})
        sample_doc = collection.find_one()
        info = {
            'document_count': doc_count,
            'sample_fields': list(sample_doc.keys()) if sample_doc else [],
            'sample': {k: type(sample_doc[k]) for k in sample_doc.keys()} if sample_doc else {}
        }
        self.logger.info(f"Collection '{collection_name}' contains {doc_count} documents")
        if sample_doc:
            self.logger.info(f"Sample fields: {info['sample_fields']}")
            self.logger.info(f"Sample: {info['sample']}")
        return info

    def flatten_document(self, doc: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
        items = []
        for k, v in doc.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self.flatten_document(v, new_key, sep=sep).items())
            elif isinstance(v, list):
                items.append((new_key, v))
            else:
                items.append((new_key, v))
        return dict(items)

    def export_to_csv(self, collection_names: List[str], output_file: Optional[str] = None,
                      flatten: bool = True, batch_size: int = 1000) -> bool:
        for collection_name in collection_names:
            if not self.collection_exists(collection_name):
                continue

            # Default output file
            out_file = output_file or f"env/{collection_name}_export.csv"
            os.makedirs(os.path.dirname(out_file), exist_ok=True)
            collection = self.db[collection_name]

            try:
                info = self.get_collection_info(collection_name)
                total_docs = info['document_count']
                if total_docs == 0:
                    self.logger.warning(f"Collection '{collection_name}' is empty")
                    continue

                self.logger.info(f"Starting export of {total_docs} documents to {out_file}")

                all_documents = []
                processed = 0

                # Export ALL documents unless we later add a --since filter specifically for CSV
                cursor = collection.find({})
                for doc in cursor:
                    if flatten:
                        doc = self.flatten_document(doc)
                        # Convert lists to strings for CSV compatibility
                        for key, value in doc.items():
                            if isinstance(value, list):
                                doc[key] = str(value)
                    if '_id' in doc:
                        doc['_id'] = str(doc['_id'])
                    all_documents.append(doc)
                    processed += 1
                    if processed % batch_size == 0:
                        self.logger.info(f"Processed {processed}/{total_docs} documents")

                df = pd.DataFrame(all_documents)
                df.to_csv(out_file, index=False)
                self.logger.info(f"Successfully exported {processed} documents to {out_file}")
                self.logger.info(f"CSV file shape: {df.shape}")

            except Exception as e:
                self.logger.error(f"Error during export: {e}")
                return False
        return True

    def close(self):
        if self.client:
            self.client.close()
            self.logger.info("MongoDB connection closed")


class MongoToPostgresSyncer:
    """
    Incrementally sync MongoDB collections into Postgres.
    - Table name = sanitized collection name (in target schema).
    - Primary key 'id' (TEXT) from Mongo _id (stringified).
    - 'updated' TIMESTAMPTZ used for incremental change detection.
    - Other fields inferred and added as columns; nested dict/list -> JSONB, arrays/objects stored as JSONB.
    """

    def __init__(
        self,
        mongo_db,
        pg_url: str,
        pg_schema: str = "public",
        updated_field: str = "updated",
        batch_size: int = 1000,
        flatten: bool = True,
        dry_run: bool = False,
        index_fields: Optional[List[str]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.mongo_db = mongo_db
        self.pg_url = pg_url
        self.pg_schema = sanitize_identifier(pg_schema)
        self.updated_field = updated_field
        self.batch_size = batch_size
        self.flatten = flatten
        self.dry_run = dry_run
        self.index_fields = [sanitize_identifier(f.strip()) for f in (index_fields or []) if f.strip()]
        self.logger = logger or setup_logger()

        self.pg_conn: Optional[psycopg2.extensions.connection] = None

    def connect_postgres(self):
        if self.pg_conn:
            return
        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would connect to Postgres: {self.pg_url}")
            return
        try:
            self.pg_conn = psycopg2.connect(self.pg_url)
            self.pg_conn.autocommit = False
            self.logger.info("Connected to Postgres")
        except psycopg2.Error as e:
            self.logger.error(f"Failed to connect to PostgreSQL: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error connecting to PostgreSQL: {e}")
            raise

    def close(self):
        if self.pg_conn:
            self.pg_conn.close()
            self.logger.info("Postgres connection closed")

    def ensure_schema(self):
        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would ensure schema exists: {self.pg_schema}")
            return
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS {quote_ident(self.pg_schema)};')
                self.pg_conn.commit()
        except psycopg2.Error as e:
            self.logger.error(f"Failed to create schema {self.pg_schema}: {e}")
            self.pg_conn.rollback()
            raise

    def ensure_table(self, collection_name: str) -> Tuple[str, str]:
        table = sanitize_identifier(collection_name)
        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would ensure table exists: {self.pg_schema}.{table}")
            return self.pg_schema, table
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {quote_ident(self.pg_schema)}.{quote_ident(table)} (
                        id TEXT PRIMARY KEY,
                        updated TIMESTAMPTZ NOT NULL
                    );
                """)
                idx_name = f'idx_{self.pg_schema}_{table}_updated'
                if len(idx_name) > 63:
                    idx_name = idx_name[:63]
                cur.execute(f"""
                    CREATE INDEX IF NOT EXISTS {quote_ident(idx_name)}
                    ON {quote_ident(self.pg_schema)}.{quote_ident(table)} (updated);
                """)
                self.pg_conn.commit()
        except psycopg2.Error as e:
            self.logger.error(f"Failed to create table {self.pg_schema}.{table}: {e}")
            self.pg_conn.rollback()
            raise
        return self.pg_schema, table

    def get_existing_columns(self, schema: str, table: str) -> Dict[str, str]:
        if self.dry_run:
            return {"id": "text", "updated": "timestamptz"}
        with self.pg_conn.cursor() as cur:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s;
            """, (schema, table))
            rows = cur.fetchall()
        cols = {r[0]: r[1] for r in rows}
        return cols

    def add_column(self, schema: str, table: str, col: str, col_type: str):
        if col in ("id", "updated"):
            return
        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would add column {schema}.{table}.{col} {col_type}")
            return
        try:
            with self.pg_conn.cursor() as cur:
                cur.execute(f"""
                    ALTER TABLE {quote_ident(schema)}.{quote_ident(table)}
                    ADD COLUMN IF NOT EXISTS {quote_ident(col)} {col_type};
                """)
            self.pg_conn.commit()
        except psycopg2.Error as e:
            self.logger.error(f"Failed to add column {schema}.{table}.{col}: {e}")
            self.pg_conn.rollback()
            raise

    def ensure_indexes(self, schema: str, table: str, existing_cols: Dict[str, str]):
        # updated index ensured in ensure_table
        for col in self.index_fields:
            if col in existing_cols:
                idx_name = f"idx_{schema}_{table}_{col}"
                if self.dry_run:
                    self.logger.info(f"[DRY-RUN] Would create index on {schema}.{table}({col})")
                    continue
                with self.pg_conn.cursor() as cur:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS {quote_ident(idx_name)}
                        ON {quote_ident(schema)}.{quote_ident(table)} ({quote_ident(col)});
                    """)
                self.pg_conn.commit()

    def flatten_document(self, doc: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
        items = []
        for k, v in doc.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(self.flatten_document(v, new_key, sep=sep).items())
            else:
                items.append((new_key, v))
        return dict(items)

    def sanitize_columns_map(self, keys: Iterable[str]) -> Dict[str, str]:
        """
        Build a mapping from original keys to sanitized unique column names.
        Handles potential collisions by suffixing with incremental numbers.
        Iterates keys in a deterministic order to keep column mappings stable across runs.
        """
        mapping: Dict[str, str] = {}
        used: Dict[str, int] = {}
        for key in sorted(keys):
            base = sanitize_identifier(key)
            name = base
            i = 1
            while name in used:
                i += 1
                name = f"{base}_{i}"
            mapping[key] = name
            used[name] = 1
        return mapping

    def get_last_updated_ts(self, schema: str, table: str) -> Optional[datetime]:
        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would query MAX(updated) from {schema}.{table}")
            return None
        with self.pg_conn.cursor() as cur:
            cur.execute(f"SELECT MAX(updated) FROM {quote_ident(schema)}.{quote_ident(table)};")
            row = cur.fetchone()
            return row[0] if row and row[0] is not None else None

    def build_mongo_filter(self, last_ts: Optional[datetime], since: Optional[datetime]) -> Dict[str, Any]:
        if last_ts:
            return {self.updated_field: {"$gt": last_ts}}
        if since:
            return {self.updated_field: {"$gte": since}}
        return {}

    def documents_batches(self, collection_name: str, last_ts: Optional[datetime], since: Optional[datetime]) -> Iterable[List[Dict[str, Any]]]:
        collection = self.mongo_db[collection_name]
        flt = self.build_mongo_filter(last_ts, since)
        self.logger.info(f"Mongo filter for '{collection_name}': {flt}")
        cursor = collection.find(flt).sort(self.updated_field, 1).batch_size(self.batch_size)
        batch: List[Dict[str, Any]] = []
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch

    def prepare_rows_and_types(
        self,
        docs: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """
        Transform Mongo docs to flattened/sanitized rows and infer column types for this batch.
        Ensures 'id' and 'updated' are set.
        """
        rows: List[Dict[str, Any]] = []
        # Flatten first to collect all keys
        flattened_docs: List[Dict[str, Any]] = []
        all_keys: set = set()

        for d in docs:
            # Convert _id to str id
            base = dict(d)
            _id = base.get("_id")
            if _id is not None:
                base["id"] = str(_id)

            # Extract updated field BEFORE flattening to preserve original field name
            updated_value = None
            if self.updated_field in base:
                updated_value = to_utc_datetime(base[self.updated_field])
                base["updated"] = updated_value

            # Flatten if requested
            flat = self.flatten_document(base) if self.flatten else base

            # Handle case where updated field might be nested and flattened
            if "updated" not in flat:
                # Look for the updated field in flattened keys
                for key, value in flat.items():
                    if key == self.updated_field or key.endswith(f".{self.updated_field}"):
                        updated_value = to_utc_datetime(value)
                        flat["updated"] = updated_value
                        break

            # Guarantee id present
            if "id" not in flat and "_id" in flat:
                flat["id"] = str(flat["_id"])

            flattened_docs.append(flat)
            all_keys.update(flat.keys())

        # Build sanitized mapping
        col_map = self.sanitize_columns_map(all_keys)
        inferred_types: Dict[str, str] = {}

        for flat in flattened_docs:
            row: Dict[str, Any] = {}
            for k, v in flat.items():
                col = col_map[k]
                val = v

                if k == "updated":
                    val = to_utc_datetime(val)
                else:
                    inferred_type = infer_pg_type(val)
                    if inferred_type == "timestamptz":
                        converted_val = to_utc_datetime(val)
                        if converted_val is not None:
                            val = converted_val

                row[col] = val
                t = infer_pg_type(val)
                if t is None:
                    continue
                if col in inferred_types:
                    inferred_types[col] = promote_type(inferred_types[col], t)
                else:
                    inferred_types[col] = t

            id_col = col_map.get("id")
            updated_col = col_map.get("updated")

            if not id_col or id_col not in row:
                self.logger.warning("Document missing _id; skipping row.")
                continue
            if not updated_col or updated_col not in row or row[updated_col] is None:
                self.logger.warning("Document missing/invalid 'updated'; skipping row with id=%s", row.get(id_col))
                continue
            rows.append(row)

        # Ensure mandatory columns types using sanitized column names
        id_col = col_map.get("id", "id")
        updated_col = col_map.get("updated", "updated")
        inferred_types[id_col] = "text"
        inferred_types[updated_col] = "timestamptz"
        return rows, inferred_types

    def normalize_pg_type(self, pg_type: str) -> str:
        """Normalize PostgreSQL type names from information_schema to our standard names."""
        type_mapping = {
            "timestamp with time zone": "timestamptz",
            "character varying": "text",
            "character": "text",
            "varchar": "text",
            "char": "text",
            "integer": "bigint",
            "int4": "bigint",
            "int8": "bigint",
            "float8": "double precision",
            "float4": "double precision",
            "real": "double precision",
            "numeric": "double precision",
            "decimal": "double precision",
            "bool": "boolean",
        }
        return type_mapping.get(pg_type.lower(), pg_type)

    def evolve_schema(self, schema: str, table: str, existing_cols: Dict[str, str], inferred_types: Dict[str, str]) -> Dict[str, str]:
        """
        Add missing columns and return refreshed existing_cols (post-evolution).
        If type mismatch is detected, we log a warning (no automatic type change).
        """
        for col, t in inferred_types.items():
            if col in existing_cols:
                existing_t = self.normalize_pg_type(existing_cols[col])
                if existing_t != t and col not in ("id", "updated"):
                    self.logger.warning(f"Detected type mismatch for column {schema}.{table}.{col}: existing {existing_t}, incoming {t}. Leaving as-is.")
                continue
            # Add new column
            self.add_column(schema, table, col, t)
        # Refresh existing cols
        return self.get_existing_columns(schema, table)

    def bulk_upsert(self, schema: str, table: str, rows: List[Dict[str, Any]], existing_cols: Dict[str, str]):
        if not rows:
            return 0, 0
        all_cols = set()
        for r in rows:
            all_cols.update(r.keys())

        id_col = None
        updated_col = None
        for col in all_cols:
            if col == "id":
                id_col = col
            if col == "updated":
                updated_col = col

        if not id_col:
            for col in all_cols:
                if col.endswith("_id") and "id" in col:
                    id_col = col
                    break
        if not updated_col:
            for col in all_cols:
                if "updated" in col:
                    updated_col = col
                    break

        if not id_col:
            id_col = "id"
        if not updated_col:
            updated_col = "updated"

        other_cols = sorted([c for c in all_cols if c not in (id_col, updated_col)])
        cols_order = [id_col, updated_col] + other_cols

        data = []
        jsonb_cols = {c for c, t in existing_cols.items() if t in ("jsonb",)}
        for r in rows:
            record = []
            for c in cols_order:
                val = r.get(c)
                if c in jsonb_cols:
                    record.append(pg_extras.Json(val))
                else:
                    record.append(val)
            data.append(tuple(record))

        columns_sql = ", ".join(quote_ident(c) for c in cols_order)
        update_sql = ", ".join(f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in cols_order if c != id_col)
        table_sql = f"{quote_ident(schema)}.{quote_ident(table)}"
        insert_sql = f"INSERT INTO {table_sql} ({columns_sql}) VALUES %s ON CONFLICT ({quote_ident(id_col)}) DO UPDATE SET {update_sql};"

        if self.dry_run:
            self.logger.info(f"[DRY-RUN] Would upsert {len(rows)} rows into {schema}.{table} with columns: {cols_order}")
            return len(rows), 0

        try:
            with self.pg_conn.cursor() as cur:
                pg_extras.execute_values(cur, insert_sql, data, page_size=self.batch_size)
            self.pg_conn.commit()
            return len(rows), 0
        except psycopg2.Error as e:
            self.logger.error(f"Failed to upsert batch into {schema}.{table}: {e}")
            self.pg_conn.rollback()
            raise

    def sync_collection(self, collection_name: str, since: Optional[datetime] = None) -> Tuple[int, int]:
        """
        Sync a single collection. Returns (total_upserted, total_batches)
        """
        self.connect_postgres()
        self.ensure_schema()
        schema, table = self.ensure_table(collection_name)
        existing_cols = self.get_existing_columns(schema, table)
        self.ensure_indexes(schema, table, existing_cols)

        last_ts = self.get_last_updated_ts(schema, table)
        total_upserted = 0
        total_batches = 0
        high_watermark: Optional[datetime] = None

        for docs in self.documents_batches(collection_name, last_ts, since):
            rows, inferred = self.prepare_rows_and_types(docs)
            if not rows:
                continue
            # Evolve schema and refresh
            existing_cols = self.evolve_schema(schema, table, existing_cols, inferred)
            # Ensure indexes again (in case newly added columns are in index_fields)
            self.ensure_indexes(schema, table, existing_cols)
            # Upsert batch
            upserted, _ = self.bulk_upsert(schema, table, rows, existing_cols)
            total_upserted += upserted
            total_batches += 1
            # Track high watermark
            valid_timestamps = [r["updated"] for r in rows if "updated" in r and r["updated"] is not None]
            if valid_timestamps:
                batch_max = max(valid_timestamps)
                if high_watermark is None or batch_max > high_watermark:
                    high_watermark = batch_max
            self.logger.info(f"Synced batch {total_batches}: upserted={upserted}, high_watermark={high_watermark}")

        self.logger.info(f"Completed sync for collection '{collection_name}': upserted={total_upserted}, batches={total_batches}, last_updated={high_watermark}")
        return total_upserted, total_batches


def main():
    """Main function to handle command line arguments and execute tasks."""
    parser = argparse.ArgumentParser(
        description="Export MongoDB collections to CSV files or sync to Postgres",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    CSV:
      python mongo_to_csv.py users
      python mongo_to_csv.py products --output products.csv

    Postgres sync:
      python mongo_to_csv.py orders customers --sync-postgres --pg-url "postgres://user:pass@localhost:5432/warehouse" --pg-schema analytics --database mystore
        """
    )

    # Core args
    parser.add_argument('collections', nargs='+', help='Names of the MongoDB collections to export/sync')
    parser.add_argument('--output', '-o', help='Output CSV file path (CSV mode only)')
    parser.add_argument('--database', '-d', default=os.getenv('MONGODB_DB', 'test'), help='MongoDB database name (default: env MONGODB_DB or "test")')
    parser.add_argument('--connection', '-c', default=os.getenv('MONGODB_URI', 'mongodb://localhost:27017/'),
                        help='MongoDB connection string (default: env MONGODB_URI or "mongodb://localhost:27017/")')
    parser.add_argument('--no-flatten', action='store_true', help='Do not flatten nested documents')
    parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for processing documents (default: 1000)')

    # Postgres sync options
    parser.add_argument('--sync-postgres', action='store_true', help='Enable Postgres sync mode instead of CSV export')
    parser.add_argument('--pg-url', default=os.getenv('POSTGRES_URL'), help='Postgres connection URL (env POSTGRES_URL supported)')
    parser.add_argument('--pg-schema', default='public', help='Target Postgres schema (default: public)')
    parser.add_argument('--updated-field', default='updated', help='Mongo field used for incremental updates (default: "updated")')
    parser.add_argument('--since', help='ISO timestamp to seed first sync when table is empty (e.g., 2024-01-01T00:00:00Z)')
    parser.add_argument('--index-fields', default='', help='Comma-separated list of additional fields to index (besides updated)')
    parser.add_argument('--dry-run', action='store_true', help='Show intended DDL/DML without executing (Postgres sync mode)')

    args = parser.parse_args()

    logger = setup_logger()

    # Create exporter instance for Mongo
    exporter = MongoToCSVExporter(
        connection_string=args.connection,
        database_name=args.database,
        logger=logger
    )

    try:
        # Connect to MongoDB
        if not exporter.connect():
            sys.exit(1)

        if args.sync_postgres:
            if not args.pg_url:
                logger.error("Postgres URL is required for --sync-postgres (provide --pg-url or POSTGRES_URL env var).")
                sys.exit(1)

            # Parse since
            since_dt: Optional[datetime] = None
            if args.since:
                since_dt = to_utc_datetime(args.since)
                if not since_dt:
                    logger.error(f"Invalid --since value: {args.since}")
                    sys.exit(1)

            # Parse index fields
            index_fields = [f.strip() for f in args.index_fields.split(',')] if args.index_fields else []

            syncer = MongoToPostgresSyncer(
                mongo_db=exporter.db,
                pg_url=args.pg_url,
                pg_schema=args.pg_schema,
                updated_field=args.updated_field,
                batch_size=args.batch_size,
                flatten=not args.no_flatten,
                dry_run=args.dry_run,
                index_fields=index_fields,
                logger=logger
            )

            total_upserted_all = 0
            total_batches_all = 0
            try:
                for coll in args.collections:
                    if not exporter.collection_exists(coll):
                        continue
                    upserted, batches = syncer.sync_collection(coll, since=since_dt)
                    total_upserted_all += upserted
                    total_batches_all += batches
                if not args.dry_run:
                    print(f"✅ Sync completed successfully! Upserted rows: {total_upserted_all} across {total_batches_all} batches.")
                else:
                    print("📝 DRY-RUN completed. Planned upserts across collections.")
            finally:
                syncer.close()
        else:
            # CSV export path
            success = exporter.export_to_csv(
                collection_names=args.collections,
                output_file=args.output,
                flatten=not args.no_flatten,
                batch_size=args.batch_size
            )
            if success:
                print("✅ Export completed successfully!")
            else:
                print("❌ Export failed!")
                sys.exit(1)

    finally:
        exporter.close()


if __name__ == "__main__":
    main()
