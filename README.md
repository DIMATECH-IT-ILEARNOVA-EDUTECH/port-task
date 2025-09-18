# MongoDB to CSV Exporter

A Python script to export MongoDB collections to CSV files for data analysis and processing.

## Features

- ✅ Check if collection exists before export
- ✅ Export all documents from a MongoDB collection to CSV
- ✅ Automatic flattening of nested documents
- ✅ Batch processing for large collections
- ✅ Command-line interface and programmatic usage
- ✅ Comprehensive logging and error handling
- ✅ Pandas DataFrame integration for data analysis

## Installation

1. Install required dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Command Line Interface

Basic usage:
```bash
python mongo_to_csv.py collection_name
```

With custom options:
```bash
python mongo_to_csv.py users --output users_export.csv --database myapp --connection mongodb://localhost:27017/
```

### Command Line Options

- `collection`: Name of the MongoDB collection to export (required)
- `--output, -o`: Output CSV file path (optional, defaults to `{collection_name}_export.csv`)
- `--database, -d`: Database name (default: `test`)
- `--connection, -c`: MongoDB connection string (default: `mongodb://localhost:27017/`)
- `--no-flatten`: Do not flatten nested documents
- `--batch-size`: Batch size for processing documents (default: 1000)

### Programmatic Usage

```python
from mongo_to_csv import MongoToCSVExporter
import pandas as pd

# Initialize exporter
exporter = MongoToCSVExporter(
    connection_string="mongodb://localhost:27017/",
    database_name="your_database"
)

# Connect and export
if exporter.connect():
    success = exporter.export_to_csv("your_collection")
    if success:
        # Load into pandas for analysis
        df = pd.read_csv("your_collection_export.csv")
        print(df.head())

exporter.close()
```

## Examples

### Export a users collection:
```bash
python mongo_to_csv.py users
```

### Export with custom output file:
```bash
python mongo_to_csv.py products --output product_data.csv
```

### Export from specific database:
```bash
python mongo_to_csv.py orders --database ecommerce
```

## Data Processing: MongoDB vs SQL

### MongoDB API Advantages:
- **Native aggregation pipeline**: Powerful for complex data transformations
- **Flexible schema**: Handles varying document structures
- **Built-in operators**: Rich set of operators for data manipulation
- **Memory efficiency**: Can process data without loading everything into memory

### SQL/DataFrame Advantages:
- **Familiar syntax**: SQL is widely known
- **Rich ecosystem**: Pandas, NumPy, scikit-learn integration
- **Visualization**: Easy integration with matplotlib, seaborn, plotly
- **Statistical analysis**: Built-in statistical functions
- **Data cleaning**: Excellent tools for handling missing data, duplicates

### Recommendation:
For **data cleaning and analysis**, using CSV + pandas/SQL is often better because:
1. **Reproducibility**: Scripts are easier to version and share
2. **Tooling**: Better IDE support and debugging for data analysis
3. **Visualization**: Seamless integration with plotting libraries
4. **Performance**: Pandas is optimized for analytical operations
5. **Flexibility**: Can easily switch between different analysis tools

Use MongoDB aggregation when:
- Data is too large to fit in memory
- You need real-time processing
- Complex document transformations are required
- You want to keep data in its native format

## Error Handling

The script includes comprehensive error handling for:
- MongoDB connection failures
- Missing collections
- Empty collections
- Data type conversion issues
- File I/O errors

## Logging

The script provides detailed logging information including:
- Connection status
- Collection existence checks
- Export progress
- Error messages with context