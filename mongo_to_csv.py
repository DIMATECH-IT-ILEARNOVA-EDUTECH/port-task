#!/usr/bin/env python3
"""
MongoDB Collection to CSV Exporter

This script connects to a MongoDB database, checks if a specified collection exists,
and exports all documents from that collection to a CSV file.

Usage:
    python mongo_to_csv.py <collection_name> [options]

Requirements:
    - pymongo
    - pandas
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging

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


class MongoToCSVExporter:
    """Handles MongoDB to CSV export operations."""

    def __init__(self, connection_string: str = "mongodb://localhost:27017/",
                 database_name: str = "test", timeout: int = 5000):
        """
        Initialize the MongoDB connection.

        Args:
            connection_string: MongoDB connection string
            database_name: Name of the database to connect to
            timeout: Connection timeout in milliseconds
        """
        self.connection_string = connection_string
        self.database_name = database_name
        self.timeout = timeout
        self.client = None
        self.db = None

        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def connect(self) -> bool:
        """
        Establish connection to MongoDB.

        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            self.client = MongoClient(
                self.connection_string,
                timeoutMS=self.timeout
            )
            # Test the connection
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
        """
        Check if a collection exists in the database.

        Args:
            collection_name: Name of the collection to check

        Returns:
            bool: True if collection exists, False otherwise
        """
        if self.db is None:
            self.logger.error("Database connection not established")
            return False

        collection_names_cursor = self.db.list_collection_names()
        collection_names = [name for name in collection_names_cursor]
        exists = collection_name in collection_names

        if exists:
            self.logger.info(f"Collection '{collection_name}' found in database")
        else:
            self.logger.warning(f"Collection '{collection_name}' not found in database")
            self.logger.info(f"Available collections: {collection_names}")

        return exists

    def get_collection_info(self, collection_name: str) -> Dict[str, Any]:
        """
        Get information about the collection.

        Args:
            collection_name: Name of the collection

        Returns:
            dict: Collection information including document count
        """
        if self.db is None:
            return {}

        collection = self.db[collection_name]
        doc_count = collection.count_documents({})

        # Get a sample document to understand the structure
        sample_doc = collection.find_one()

        info = {
            'document_count': doc_count,
            'sample_fields': list(sample_doc.keys()) if sample_doc else []
        }

        self.logger.info(f"Collection '{collection_name}' contains {doc_count} documents")
        if sample_doc:
            self.logger.info(f"Sample fields: {info['sample_fields']}")

        return info

    def flatten_document(self, doc: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
        """
        Flatten nested dictionaries in MongoDB documents.

        Args:
            doc: Document to flatten
            parent_key: Parent key for nested fields
            sep: Separator for nested field names

        Returns:
            dict: Flattened document
        """
        items = []
        for k, v in doc.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k

            if isinstance(v, dict):
                items.extend(self.flatten_document(v, new_key, sep=sep).items())
            elif isinstance(v, list):
                # Convert lists to string representation
                items.append((new_key, str(v)))
            else:
                items.append((new_key, v))

        return dict(items)

    def export_to_csv(self, collection_name: str, output_file: Optional[str] = None,
                     flatten: bool = True, batch_size: int = 1000) -> bool:
        """
        Export collection to CSV file.

        Args:
            collection_name: Name of the collection to export
            output_file: Output CSV file path (optional)
            flatten: Whether to flatten nested documents
            batch_size: Number of documents to process at once

        Returns:
            bool: True if export successful, False otherwise
        """
        if not self.collection_exists(collection_name):
            return False

        # Set default output file name
        if not output_file:
            output_file = f"{collection_name}_export.csv"

        collection = self.db[collection_name]

        try:
            # Get collection info
            info = self.get_collection_info(collection_name)
            total_docs = info['document_count']

            if total_docs == 0:
                self.logger.warning(f"Collection '{collection_name}' is empty")
                return False

            self.logger.info(f"Starting export of {total_docs} documents to {output_file}")

            # Process documents in batches
            all_documents = []
            processed = 0

            cursor = collection.find()

            for doc in cursor:
                if flatten:
                    doc = self.flatten_document(doc)

                # Convert ObjectId to string
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])

                all_documents.append(doc)
                processed += 1

                if processed % batch_size == 0:
                    self.logger.info(f"Processed {processed}/{total_docs} documents")

            # Convert to DataFrame and save as CSV
            df = pd.DataFrame(all_documents)
            df.to_csv(output_file, index=False)

            self.logger.info(f"Successfully exported {processed} documents to {output_file}")
            self.logger.info(f"CSV file shape: {df.shape}")

            return True

        except Exception as e:
            self.logger.error(f"Error during export: {e}")
            return False

    def close(self):
        """Close the MongoDB connection."""
        if self.client:
            self.client.close()
            self.logger.info("MongoDB connection closed")


def main():
    """Main function to handle command line arguments and execute export."""
    parser = argparse.ArgumentParser(
        description="Export MongoDB collection to CSV file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python mongo_to_csv.py users
    python mongo_to_csv.py products --output products.csv
    python mongo_to_csv.py orders --database mystore --connection mongodb://localhost:27017/
        """
    )

    parser.add_argument('collection', help='Name of the MongoDB collection to export')
    parser.add_argument('--output', '-o', help='Output CSV file path')
    parser.add_argument('--database', '-d', default='test', help='Database name (default: test)')
    parser.add_argument('--connection', '-c', default='mongodb://localhost:27017/',
                       help='MongoDB connection string (default: mongodb://localhost:27017/)')
    parser.add_argument('--no-flatten', action='store_true',
                       help='Do not flatten nested documents')
    parser.add_argument('--batch-size', type=int, default=1000,
                       help='Batch size for processing documents (default: 1000)')

    args = parser.parse_args()

    # Create exporter instance
    exporter = MongoToCSVExporter(
        connection_string=args.connection,
        database_name=args.database
    )

    try:
        # Connect to MongoDB
        if not exporter.connect():
            sys.exit(1)

        # Export collection
        success = exporter.export_to_csv(
            collection_name=args.collection,
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
