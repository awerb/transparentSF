import os
import json
import logging
import sys
import psycopg2
import psycopg2.extras
import pandas as pd
import requests
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
import traceback
import base64
from io import BytesIO
from PIL import Image
import time
import re
from dateutil.relativedelta import relativedelta
import plotly.graph_objects as go
import plotly.io as pio
from plotly.subplots import make_subplots

# Set up paths to look for .env file
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)

# Add the project root and script directory to the Python path
sys.path.insert(0, project_root)
sys.path.insert(0, script_dir)

possible_env_paths = [
    os.path.join(script_dir, '.env'),  # ai/.env
    os.path.join(project_root, '.env'),  # /.env (project root)
    os.path.join(os.path.expanduser('~'), '.env')  # ~/.env (home directory)
]

# Try loading .env from each location
for env_path in possible_env_paths:
    if os.path.exists(env_path):
        print(f"Loading environment variables from: {env_path}")
        load_dotenv(env_path)
        break

# Configure logging - IMPORTANT: Do this before importing any other modules
# that might configure their own loggers

# Get logging level from environment or default to INFO
log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)

# Create logs directory if it doesn't exist
logs_dir = os.path.join(script_dir, 'logs')
os.makedirs(logs_dir, exist_ok=True)

# Configure file handler with absolute path
log_file = os.path.join(logs_dir, 'monthly_report.log')
file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
file_handler.setLevel(log_level)  # Set handler level to match log_level

# Configure console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(log_level)  # Set handler level to match log_level

# Create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Configure the root logger
root_logger = logging.getLogger()
root_logger.setLevel(log_level)  # Set root logger level to match log_level

# Remove any existing handlers from the root logger to avoid duplicate logs
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Add handlers to root logger
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

# Create a module-specific logger that will use the root logger's configuration
logger = logging.getLogger(__name__)

# Now log initialization messages
logger.warning(f"Monthly report logging initialized with level: {log_level_name}")
logger.info("This is an INFO level message - should not appear if LOG_LEVEL is WARNING")
logger.warning(f"Log file location: {log_file}")
logger.debug("This is a DEBUG level message - should never appear if LOG_LEVEL is WARNING")
logger.warning(f"Current working directory: {os.getcwd()}")
logger.warning(f"Script directory: {script_dir}")
logger.warning(f"Project root: {project_root}")
logger.info(f"Python path: {sys.path}")  # This should not appear if LOG_LEVEL is WARNING

# Test write access to log file
try:
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"\nTest write at {datetime.now()}\n")
    logger.warning(f"Successfully wrote to log file: {log_file}")
except Exception as e:
    logger.error(f"Error writing to log file: {e}")

# Prevent imported modules from modifying our logging configuration
# This is done by defining a no-op basicConfig function
original_basic_config = logging.basicConfig
def no_op_basic_config(*args, **kwargs):
    # If an imported module tries to configure logging, we'll maintain our settings
    # But we'll log it at debug level to help troubleshoot if needed
    logger.debug(f"Ignored attempt to call logging.basicConfig with args: {args}, kwargs: {kwargs}")
    
    # If the caller is trying to set a level, we'll check against our level
    if 'level' in kwargs:
        requested_level = kwargs['level']
        logger.debug(f"Module requested logging level: {requested_level}, our level: {log_level}")
    
logging.basicConfig = no_op_basic_config

# Import database utilities
try:
    # First try to import from the ai package
    from ai.tools.db_utils import get_postgres_connection, execute_with_connection, CustomJSONEncoder
    logger.warning("Successfully imported from ai.tools.db_utils")
except ImportError:
    try:
        # If that fails, try to import from the local directory
        from tools.db_utils import get_postgres_connection, execute_with_connection, CustomJSONEncoder
        logger.warning("Successfully imported from tools.db_utils")
    except ImportError:
        logger.error("Failed to import from db_utils", exc_info=True)
        raise

# Import necessary functions from other modules
try:
    # First try to import from the ai package
    from ai.webChat import get_dashboard_metric, anomaly_explainer_agent, swarm_client, context_variables, client, AGENT_MODEL, load_and_combine_notes
    logger.warning("Successfully imported from ai.webChat")
except ImportError:
    try:
        # If that fails, try to import from the local directory
        from webChat import get_dashboard_metric, anomaly_explainer_agent, swarm_client, context_variables, client, AGENT_MODEL, load_and_combine_notes
        logger.warning("Successfully imported from webChat")
    except ImportError:
        logger.error("Failed to import from webChat", exc_info=True)
        raise

# Restore original basicConfig if needed
logging.basicConfig = original_basic_config

# Load environment variables
load_dotenv()

# Only log non-sensitive environment variables
non_sensitive_vars = {k: v for k, v in os.environ.items() 
                     if k.startswith('POSTGRES_') and not k.endswith('PASSWORD')}
logger.warning(f"Environment variables (excluding sensitive data): {non_sensitive_vars}")

# Database connection parameters - use POSTGRES_* variables directly
DB_HOST = os.getenv("POSTGRES_HOST", 'localhost')
DB_PORT = os.getenv("POSTGRES_PORT", '5432')
DB_USER = os.getenv("POSTGRES_USER", 'postgres')
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", 'postgres')
DB_NAME = os.getenv("POSTGRES_DB", 'transparentsf')

# Log only non-sensitive connection parameters
logger.warning(f"Using database connection parameters: HOST={DB_HOST}, PORT={DB_PORT}, USER={DB_USER}, DB={DB_NAME}")

# Validate and convert DB_PORT to int if it exists
try:
    DB_PORT = int(DB_PORT)
except ValueError:
    logger.error(f"Invalid POSTGRES_PORT value: {DB_PORT}. Must be an integer.")
    DB_PORT = 5432  # Default port

# API Base URL
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
logger.warning(f"Using API_BASE_URL: {API_BASE_URL}")

# Perplexity API Key
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
if PERPLEXITY_API_KEY:
    logger.warning("Perplexity API key found")
else:
    logger.warning("No Perplexity API key found. Perplexity API features will not be available.")

# Global variable to store prompts
_PROMPTS = None

def load_prompts():
    """
    Load prompts from the JSON file and store them in the global _PROMPTS variable.
    Only loads the prompts once.
    
    Returns:
        Dictionary containing all prompts
    """
    global _PROMPTS
    
    if _PROMPTS is None:
        try:
            prompts_path = Path(__file__).parent / 'data' / 'prompts.json'
            logger.info(f"Loading prompts from {prompts_path}")
            
            with open(prompts_path, 'r') as f:
                _PROMPTS = json.load(f)
                
            logger.info(f"Successfully loaded prompts with keys: {list(_PROMPTS.keys())}")
        except Exception as e:
            logger.error(f"Error loading prompts: {str(e)}")
            raise
    
    return _PROMPTS

def initialize_monthly_reporting_table():
    """
    Initialize the monthly_reporting table in PostgreSQL if it doesn't exist.
    Also creates the reports table if it doesn't exist.
    """
    logger.info("Initializing tables for monthly reporting")
    
    def init_table_operation(connection):
        """Inner function to initialize the tables within a connection."""
        cursor = connection.cursor()
        
        # First, check and create the reports table if it doesn't exist
        logger.info("Checking if reports table exists...")
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'reports'
            );
        """)
        reports_table_exists = cursor.fetchone()[0]
        
        if not reports_table_exists:
            logger.info("Creating reports table...")
            try:
                cursor.execute("""
                    CREATE TABLE reports (
                        id SERIAL PRIMARY KEY,
                        max_items INTEGER DEFAULT 10,
                        district TEXT DEFAULT '0',
                        period_type TEXT DEFAULT 'month',
                        original_filename TEXT,
                        revised_filename TEXT,
                        published_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                connection.commit()
                logger.info("Reports table created successfully")
            except Exception as e:
                logger.error(f"Error creating reports table: {e}")
                connection.rollback()
                return False
        else:
            logger.info("Reports table already exists")
            
            # Check if the published_url column exists in the reports table
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'reports' AND column_name = 'published_url'
            """)
            
            published_url_exists = cursor.fetchone()
            
            if not published_url_exists:
                logger.info("Adding published_url column to reports table")
                try:
                    cursor.execute("""
                        ALTER TABLE reports ADD COLUMN published_url TEXT
                    """)
                    connection.commit()
                    logger.info("Added published_url column to reports table")
                except Exception as e:
                    logger.error(f"Error adding published_url column: {e}")
                    connection.rollback()

            # Check if the proofread_feedback column exists in the reports table
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'reports' AND column_name = 'proofread_feedback'
            """)
            proofread_feedback_exists = cursor.fetchone()

            if not proofread_feedback_exists:
                logger.info("Adding proofread_feedback column to reports table")
                try:
                    cursor.execute("""
                        ALTER TABLE reports ADD COLUMN proofread_feedback TEXT
                    """)
                    connection.commit()
                    logger.info("Added proofread_feedback column to reports table")
                except Exception as e:
                    logger.error(f"Error adding proofread_feedback column: {e}")
                    connection.rollback()

            # Check if the headlines column exists in the reports table
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'reports' AND column_name = 'headlines'
            """)
            headlines_exists = cursor.fetchone()

            if not headlines_exists:
                logger.info("Adding headlines column to reports table")
                try:
                    cursor.execute("""
                        ALTER TABLE reports ADD COLUMN headlines JSONB
                    """)
                    connection.commit()
                    logger.info("Added headlines column to reports table")
                except Exception as e:
                    logger.error(f"Error adding headlines column: {e}")
                    connection.rollback()
        
        # Now check if the monthly_reporting table exists and get its columns
        cursor.execute("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'monthly_reporting'
            ORDER BY ordinal_position
        """)
        
        existing_columns = [col[0] for col in cursor.fetchall()]
        
        # If the table exists but with a different schema, we need to handle it
        if existing_columns:
            logger.info(f"Table monthly_reporting exists with columns: {existing_columns}")
            
            # Check if we have the required columns for our new schema
            required_columns = [
                "id", "report_date", "metric_name", "group_value", 
                "group_field_name", "period_type", "comparison_mean", 
                "recent_mean", "difference", "std_dev", "percent_change", 
                "explanation", "priority", "report_text", "district", 
                "chart_data", "metadata", "created_at", "metric_id", "report_id", "item_title"
            ]
            
            missing_columns = [col for col in required_columns if col not in existing_columns]
            
            if missing_columns:
                logger.info(f"Adding missing columns to monthly_reporting table: {missing_columns}")
                
                # Add missing columns one by one
                for column in missing_columns:
                    try:
                        if column == "id":
                            # Skip id as it's likely already the primary key
                            continue
                        elif column == "report_date":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN report_date DATE DEFAULT CURRENT_DATE")
                        elif column == "metric_name":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN metric_name TEXT")
                        elif column == "group_value":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN group_value TEXT")
                        elif column == "group_field_name":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN group_field_name TEXT")
                        elif column == "period_type":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN period_type TEXT DEFAULT 'month'")
                        elif column == "comparison_mean":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN comparison_mean FLOAT")
                        elif column == "recent_mean":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN recent_mean FLOAT")
                        elif column == "difference":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN difference FLOAT")
                        elif column == "std_dev":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN std_dev FLOAT")
                        elif column == "percent_change":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN percent_change FLOAT")
                        elif column == "explanation":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN explanation TEXT")
                        elif column == "priority":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN priority INTEGER")
                        elif column == "report_text":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN report_text TEXT")
                        elif column == "district":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN district TEXT")
                        elif column == "chart_data":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN chart_data JSONB")
                        elif column == "metadata":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN metadata JSONB")
                        elif column == "created_at":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        elif column == "metric_id":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN metric_id TEXT")
                        elif column == "report_id":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN report_id INTEGER REFERENCES reports(id) ON DELETE CASCADE")
                        elif column == "item_title":
                            cursor.execute("ALTER TABLE monthly_reporting ADD COLUMN item_title TEXT")

                    except Exception as e:
                        logger.warning(f"Error adding column {column}: {e}")
                        # Continue with other columns even if one fails
                
                connection.commit()
                logger.info("Added missing columns to monthly_reporting table")
            else:
                logger.info("Table monthly_reporting already has all required columns")
        else:
            # Create monthly_reporting table if it doesn't exist
            logger.info("Creating monthly_reporting table...")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS monthly_reporting (
                    id SERIAL PRIMARY KEY,
                    report_id INTEGER REFERENCES reports(id) ON DELETE CASCADE,
                    report_date DATE DEFAULT CURRENT_DATE,
                    item_title TEXT,
                    metric_name TEXT,
                    metric_id TEXT,
                    group_value TEXT,
                    group_field_name TEXT,
                    period_type TEXT DEFAULT 'month',
                    comparison_mean FLOAT,
                    recent_mean FLOAT,
                    difference FLOAT,
                    std_dev FLOAT,
                    percent_change FLOAT,
                    explanation TEXT,
                    priority INTEGER,
                    report_text TEXT,
                    district TEXT,
                    chart_data JSONB,
                    metadata JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS monthly_reporting_report_date_idx ON monthly_reporting (report_date)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS monthly_reporting_district_idx ON monthly_reporting (district)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS monthly_reporting_priority_idx ON monthly_reporting (priority)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS monthly_reporting_report_id_idx ON monthly_reporting (report_id)
            """)
            
            connection.commit()
            logger.info("Monthly reporting table created successfully")
        
        cursor.close()
        return True
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=init_table_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] == "success":
        return True
    else:
        logger.error(f"Failed to initialize tables: {result['message']}")
        return False

def select_deltas_to_discuss(period_type='month', top_n=20, bottom_n=20, district=""):
    """
    Step 1: Select the top and bottom N differences to discuss.
    Uses the anomaly_analyzer API endpoint to find the most significant changes.
    
    Args:
        period_type: The time period type (month, quarter, year)
        top_n: Number of top increasing differences to select
        bottom_n: Number of top decreasing differences to select
        district: District number to analyze
        
    Returns:
        Dictionary with top and bottom differences
    """
    logger.info(f"Selecting top {top_n} and bottom {bottom_n} deltas for {period_type}ly report")
    
    try:
        # Use the top-metric-changes API endpoint to get top changes
        top_url = f"{API_BASE_URL}/anomaly-analyzer/api/top-metric-changes?period_type={period_type}&limit={top_n}&district={district}&show_both=false"
        logger.info(f"Requesting top changes from: {top_url}")
        
        top_response = requests.get(top_url)
        logger.info(f"Top changes API response status code: {top_response.status_code}")
        
        if top_response.status_code != 200:
            error_msg = f"Failed to get top changes: Status code {top_response.status_code} - {top_response.text}"
            logger.error(error_msg)
            return {"status": "error", "message": error_msg}
        
        try:
            top_data = top_response.json()
            logger.info(f"Received top changes data: {json.dumps(top_data)[:200]}...")
            
            # Log more details about the returned data structure
            if isinstance(top_data, dict):
                logger.info(f"Top data keys: {list(top_data.keys())}")
                if "top_results" in top_data:
                    logger.info(f"Top results count: {len(top_data['top_results'])}")
                    if top_data['top_results'] and len(top_data['top_results']) > 0:
                        sample_item = top_data['top_results'][0]
                        logger.info(f"Sample top result item keys: {list(sample_item.keys())}")
                        logger.info(f"Sample top result item: {json.dumps(sample_item)}")
        except Exception as e:
            logger.error(f"Error parsing top changes response as JSON: {str(e)}")
            logger.error(f"Raw response content: {top_response.text[:500]}")
            raise
        
        # Use the same endpoint with negative=true for bottom changes
        bottom_url = f"{API_BASE_URL}/anomaly-analyzer/api/top-metric-changes?period_type={period_type}&limit={bottom_n}&district={district}&show_both=false&negative=true"
        logger.info(f"Requesting bottom changes from: {bottom_url}")
        
        bottom_response = requests.get(bottom_url)
        logger.info(f"Bottom changes API response status code: {bottom_response.status_code}")
        
        if bottom_response.status_code != 200:
            error_msg = f"Failed to get bottom changes: Status code {bottom_response.status_code} - {bottom_response.text}"
            logger.error(error_msg)
            return {"status": "error", "message": error_msg}
        
        try:
            bottom_data = bottom_response.json()
            logger.info(f"Received bottom changes data: {json.dumps(bottom_data)[:200]}...")
            
            # Log more details about the returned data structure
            if isinstance(bottom_data, dict):
                logger.info(f"Bottom data keys: {list(bottom_data.keys())}")
                if "bottom_results" in bottom_data:
                    logger.info(f"Bottom results count: {len(bottom_data['bottom_results'])}")
                    if bottom_data['bottom_results'] and len(bottom_data['bottom_results']) > 0:
                        sample_item = bottom_data['bottom_results'][0]
                        logger.info(f"Sample bottom result item keys: {list(sample_item.keys())}")
                        logger.info(f"Sample bottom result item: {json.dumps(sample_item)}")
        except Exception as e:
            logger.error(f"Error parsing bottom changes response as JSON: {str(e)}")
            logger.error(f"Raw response content: {bottom_response.text[:500]}")
            raise
        
        # Format the results from the API to match what we need
        top_changes = []
        for item in top_data.get("top_results", []):
            try:
                # Safely handle None values
                recent_value = float(item.get("recent_value", 0)) if item.get("recent_value") is not None else 0
                previous_value = float(item.get("previous_value", 0)) if item.get("previous_value") is not None else 0
                difference = recent_value - previous_value
                
                top_changes.append({
                    "metric": item.get("object_name", "Unknown"),
                    "metric_id": item.get("object_id", "Unknown"),
                    "group": item.get("group", "All"),
                    "recent_mean": recent_value,
                    "comparison_mean": previous_value,
                    "difference_value": difference,
                    "difference": difference,
                    "district": district
                })
            except (ValueError, TypeError) as e:
                logger.error(f"Error processing top change item: {e}")
                logger.error(f"Problematic item data: {json.dumps(item)}")
                continue
        
        bottom_changes = []
        for item in bottom_data.get("bottom_results", []):
            try:
                # Safely handle None values
                recent_value = float(item.get("recent_value", 0)) if item.get("recent_value") is not None else 0
                previous_value = float(item.get("previous_value", 0)) if item.get("previous_value") is not None else 0
                difference = recent_value - previous_value
                
                bottom_changes.append({
                    "metric": item.get("object_name", "Unknown"),
                    "metric_id": item.get("object_id", "Unknown"),
                    "group": item.get("group", "All"),
                    "recent_mean": recent_value,
                    "comparison_mean": previous_value,
                    "difference_value": difference,
                    "difference": difference,
                    "district": district
                })
            except (ValueError, TypeError) as e:
                logger.error(f"Error processing bottom change item: {e}")
                logger.error(f"Problematic item data: {json.dumps(item)}")
                continue
        
        # Combine results
        all_deltas = {
            "top_changes": top_changes,
            "bottom_changes": bottom_changes,
            "status": "success",
            "period_type": period_type,
            "district": district
        }
        
        logger.info(f"Found {len(all_deltas['top_changes'])} top changes and {len(all_deltas['bottom_changes'])} bottom changes")
        return all_deltas
        
    except Exception as e:
        error_msg = f"Error in select_deltas_to_discuss: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def prioritize_deltas(deltas, max_items=10):
    """
    Step 2: Prioritize the deltas for discussion based on their importance.
    This uses the explainer agent to determine which changes are most significant.
    
    Args:
        deltas: Dictionary with top and bottom changes from select_deltas_to_discuss
        max_items: Maximum number of items to prioritize
        
    Returns:
        List of prioritized items with explanations
    """
    from webChat import client, AGENT_MODEL, load_and_combine_notes
    
    logger.info(f"Prioritizing deltas for discussion (max {max_items} items)")
    
    try:
        if deltas.get("status") != "success":
            return {"status": "error", "message": "Invalid deltas input"}
        
        # Combine top and bottom changes
        combined_changes = deltas.get("top_changes", []) + deltas.get("bottom_changes", [])
        
        # Get combined notes with YTD metrics for reference
        notes_text = load_and_combine_notes()
        logger.info(f"Loaded {len(notes_text)} characters of combined notes for context")
        
        # Format changes for the agent to analyze
        changes_text = "Here are the metrics with significant changes:\n\n"
        for idx, change in enumerate(combined_changes, 1):
            metric = change.get("metric", "Unknown")
            metric_id = change.get("metric_id", "Unknown")
            group = change.get("group", "Unknown")
            # Safely handle None values for group
            if group is None:
                group = "All"
            diff = change.get("difference_value", 0)
            recent = change.get("recent_mean", 0)
            comparison = change.get("comparison_mean", 0)
            district = change.get("district", "0")
            
            # Add an index field to each change for reference
            change["index"] = idx
            
            # Calculate percent change
            if comparison != 0:
                pct_change = (diff / comparison) * 100
                pct_text = f"{pct_change:+.1f}%"
            else:
                pct_text = "N/A"
            
            changes_text += f"{idx}. {metric} for {group}: {recent:.1f} vs {comparison:.1f} ({pct_text}), District: {district}\n"
        
        # Load prompt from JSON file
        prompts = load_prompts()
        prompt_template = prompts['monthly_report']['prioritize_deltas']['prompt']
        system_message = prompts['monthly_report']['prioritize_deltas']['system']
        
        # Truncate notes_text for the prompt
        notes_text_short = notes_text[:5000]
        
        # Format the prompt with the required variables
        prompt = prompt_template.format(
            max_items=max_items,
            changes_text=changes_text,
            notes_text_short=notes_text_short
        )

        # Make API call to get prioritized list
        response = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[{"role": "system", "content": system_message},
                     {"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        response_content = response.choices[0].message.content
        logger.info(f"Received JSON response of length: {len(response_content)}")
        
        try:
            # Parse the JSON response
            prioritized_json = json.loads(response_content)
            logger.info(f"Parsed JSON: {json.dumps(prioritized_json)[:200]}...")
            
            # Extract the array if the response is wrapped in an object
            prioritized_items_raw = []
            if isinstance(prioritized_json, dict):
                # Try different possible keys that might contain the items array
                for key in ["items", "prioritized_items", "metrics", "results", "data"]:
                    if key in prioritized_json:
                        prioritized_items_raw = prioritized_json.get(key, [])
                        logger.info(f"Found items under key: {key}")
                        break
                
                # If no known key was found but there's a single array in the dict, use it
                if not prioritized_items_raw:
                    for value in prioritized_json.values():
                        if isinstance(value, list):
                            prioritized_items_raw = value
                            logger.info(f"Found items array as a value")
                            break
                            
                # If still no array, treat the entire object as a single-item array if it has 'metric'
                if not prioritized_items_raw and "metric" in prioritized_json:
                    prioritized_items_raw = [prioritized_json]
                    logger.info("Treating entire object as a single item")
            elif isinstance(prioritized_json, list):
                prioritized_items_raw = prioritized_json
                logger.info("Found direct array of items")
            else:
                logger.warning(f"Unexpected JSON structure: {type(prioritized_json)}")
                
            # Print what we found for debugging
            logger.info(f"Extracted {len(prioritized_items_raw)} items from JSON")
            for i, item in enumerate(prioritized_items_raw[:3]):  # Print first 3 for debugging
                logger.info(f"  Item {i+1}: {json.dumps(item)[:100]}...")
            
            # Process each item to match with original data
            prioritized_items = []
            for item in prioritized_items_raw:
                # Get the item index to match with original data
                item_index = item.get("index")
                if item_index is not None:
                    # Find the original change data by index
                    original_change = None
                    for change in combined_changes:
                        if change.get("index") == item_index:
                            original_change = change
                            break
                    
                    if original_change:
                        logger.info(f"Found original data for item index {item_index}: {original_change.get('metric')}")
                        # Create prioritized item with data from both the AI response and original change
                        prioritized_items.append({
                            "metric": original_change.get("metric"),
                            "metric_id": original_change.get("metric_id", "Unknown"),
                            "group": original_change.get("group", "All"),
                            "priority": item.get("priority", 999),
                            "recent_mean": original_change.get("recent_mean", 0),
                            "comparison_mean": original_change.get("comparison_mean", 0),
                            "difference": original_change.get("difference_value", 0),
                            "district": original_change.get("district", "0"),
                            "explanation": item.get("explanation", ""),
                            "trend_analysis": item.get("trend_analysis", ""),
                            "follow_up": item.get("follow_up", "")
                        })
                    else:
                        logger.warning(f"Could not find original data for item index {item_index}")
                else:
                    # Fallback to matching by name if no index
                    metric_name = item.get("metric", "").strip()
                    group_value = item.get("group", "All").strip()
                    
                    # Find the original change data
                    original_change = None
                    for change in combined_changes:
                        if change.get("metric") == metric_name and (change.get("group") == group_value or (change.get("group") is None and group_value == "All")):
                            original_change = change
                            break
                    
                    if original_change:
                        logger.info(f"Found original data for metric: {metric_name} by name matching")
                        # Create prioritized item with data from both the AI response and original change
                        prioritized_items.append({
                            "metric": metric_name,
                            "metric_id": original_change.get("metric_id", "Unknown"),
                            "group": group_value,
                            "priority": item.get("priority", 999),
                            "recent_mean": original_change.get("recent_mean", 0),
                            "comparison_mean": original_change.get("comparison_mean", 0),
                            "difference": original_change.get("difference_value", 0),
                            "district": original_change.get("district", "0"),
                            "explanation": item.get("explanation", ""),
                            "trend_analysis": item.get("trend_analysis", ""),
                            "follow_up": item.get("follow_up", "")
                        })
                    else:
                        logger.warning(f"Could not find original data for metric: {metric_name}, group: {group_value}")
            
            # Sort by priority
            prioritized_items.sort(key=lambda x: x.get("priority", 999))
            
            # Log prioritized items
            for item in prioritized_items:
                logger.info(f"Prioritized item: {item.get('metric')} - {item.get('group')} (Priority: {item.get('priority')})")
                logger.info(f"  Explanation length: {len(item.get('explanation', ''))}")
                logger.info(f"  Trend analysis: {item.get('trend_analysis', '')[:50]}...")
            
            logger.info(f"Prioritized {len(prioritized_items)} items for the report")
            return {"status": "success", "prioritized_items": prioritized_items}
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            logger.error(f"Raw response: {response_content[:200]}...")
            return {"status": "error", "message": f"Failed to parse JSON response: {e}"}
        
    except Exception as e:
        error_msg = f"Error in prioritize_deltas: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def store_prioritized_items(prioritized_items, period_type='month', district="0"):
    """
    Store the prioritized items in the monthly_reporting table
    
    Args:
        prioritized_items: List of prioritized items with explanations
        period_type: The time period type (month, quarter, year)
        district: District number
        
    Returns:
        Status dictionary
    """
    logger.info(f"Storing {len(prioritized_items)} prioritized items in the database")
    
    if not prioritized_items or not isinstance(prioritized_items, list):
        return {"status": "error", "message": "Invalid prioritized items"}
    
    def store_items_operation(connection):
        cursor = connection.cursor()
        
        # Get the current date for the report
        report_date = datetime.now().date()
        
        # Generate filenames for the report
        original_filename = f"monthly_report_{district}_{report_date.strftime('%Y_%m')}.html"
        revised_filename = f"monthly_report_{district}_{report_date.strftime('%Y_%m')}_revised.html"
        
        # Create a record in the reports table first
        cursor.execute("""
            INSERT INTO reports (
                max_items, district, period_type, original_filename, revised_filename,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id
        """, (
            len(prioritized_items),
            district,
            period_type,
            original_filename,
            revised_filename
        ))
        
        report_id = cursor.fetchone()[0]
        logger.info(f"Created report record with ID: {report_id}")
        
        # Insert each prioritized item
        inserted_ids = []
        for item in prioritized_items:
            # Calculate percent change
            comparison_mean = item.get("comparison_mean", 0)
            if comparison_mean != 0:
                percent_change = (item.get("difference", 0) / comparison_mean) * 100
            else:
                percent_change = 0
                
            # Log the data before insertion
            logger.info(f"Storing item: {item.get('metric')} - Priority: {item.get('priority')}")
            logger.info(f"  Explanation: '{item.get('explanation', '')[:50]}...' (length: {len(item.get('explanation', ''))})")
            logger.info(f"  Trend analysis: '{item.get('trend_analysis', '')[:50]}...'")
            
            # Create metadata JSON to store additional fields
            metadata = {
                "trend_analysis": item.get("trend_analysis", ""),
                "follow_up": item.get("follow_up", "")
            }
            
            # Create item title from metric name and group
            metric_name = item.get("metric", "")
            metric_id = item.get("metric_id", "Unknown")
            group_value = item.get("group", "All")
            item_title = f"{metric_name} - {group_value}" if group_value != "All" else metric_name
                
            # Prepare the data
            cursor.execute("""
                INSERT INTO monthly_reporting (
                    report_id, report_date, item_title, metric_name, metric_id, group_value, group_field_name, 
                    period_type, comparison_mean, recent_mean, difference, 
                    percent_change, explanation, priority, district, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) RETURNING id
            """, (
                report_id,
                report_date,
                item_title,
                metric_name,
                metric_id,
                group_value,
                "group", # Default group field name
                period_type,
                item.get("comparison_mean", 0),
                item.get("recent_mean", 0),
                item.get("difference", 0),
                percent_change,
                item.get("explanation", ""),
                item.get("priority", 999),
                item.get("district", district),
                json.dumps(metadata)
            ))
            
            inserted_id = cursor.fetchone()[0]
            inserted_ids.append(inserted_id)
            
            # Check if the insertion worked
            cursor.execute("SELECT LENGTH(explanation) FROM monthly_reporting WHERE id = %s", (inserted_id,))
            expl_length = cursor.fetchone()[0]
            logger.info(f"Stored item with ID {inserted_id}, explanation length in DB: {expl_length}")
        
        connection.commit()
        cursor.close()
        
        logger.info(f"Successfully stored {len(inserted_ids)} items in the database")
        return inserted_ids
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=store_items_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] == "success":
        return {"status": "success", "inserted_ids": result["result"]}
    else:
        return {"status": "error", "message": result["message"]}

def generate_explanations(report_ids):
    """
    Step 3: Generate detailed explanations for each prioritized item using the explainer agent.
    Updates the monthly_reporting table with the explanations.
    
    Args:
        report_ids: List of report IDs to explain
        
    Returns:
        Status dictionary
    """
    logger.info(f"Generating explanations for {len(report_ids)} items")
    
    def generate_explanations_operation(connection):
        cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        successful_explanations = 0
        
        for report_id in report_ids:
            # Get the report item
            cursor.execute("""
                SELECT * FROM monthly_reporting WHERE id = %s
            """, (report_id,))
            
            item = cursor.fetchone()
            if not item:
                logger.warning(f"Report item with ID {report_id} not found")
                continue
            
            # Format the data for the explainer agent
            metric_name = item["metric_name"]
            metric_id = item["metric_id"]  # Get the metric_id from the item
            group_value = item["group_value"]
            recent_mean = item["recent_mean"]
            comparison_mean = item["comparison_mean"]
            difference = item["difference"]
            percent_change = item["percent_change"]
            district = item["district"]
            period_type = item["period_type"]
            
            # Calculate time periods for context - check the report date
            report_date = item.get("report_date") or datetime.now().date()
            if isinstance(report_date, str):
                report_date = datetime.strptime(report_date, "%Y-%m-%d").date()
            
            # Get the previous month's date (the more recent month in the comparison)
            # Using a safer approach to handle edge cases with month days
            previous_month_date = report_date - relativedelta(months=1)
            
            # Get the month before the previous month (the earlier month in the comparison)
            # Using a safer approach to handle edge cases with month days
            comparison_month_date = previous_month_date - relativedelta(months=1)
            
            # Format the month names
            recent_month = previous_month_date.strftime("%B %Y")
            previous_month = comparison_month_date.strftime("%B %Y")
            
            logger.info(f"Comparing data between {previous_month} and {recent_month}")
            
            # Calculate delta if not already in the item
            delta = item.get("difference", recent_mean - comparison_mean)
            
            # Add detailed context about the item for logging
            logger.info(f"Generating explanation for report_id={report_id}, metric={metric_name} (ID: {metric_id}), group={group_value}")
            logger.info(f"  Values: recent={recent_mean}, comparison={comparison_mean}, diff={difference}, percent_change={percent_change}")
            logger.info(f"  Period: {period_type}, District: {district}")
            
            # Create a simple session for the anomaly explainer agent
            session_id = f"report_{report_id}"
            session_context = context_variables.copy()
            
            # Create a session for the agent
            sessions = {}
            sessions[session_id] = {
                "messages": [],
                "agent": anomaly_explainer_agent,
                "context_variables": session_context
            }
            
            # Create a simple, direct prompt for the agent
            direction = "increased" if delta > 0 else "decreased"
            percent_change_str = f"{abs(percent_change):.2f}%" if percent_change is not None else "unknown percentage"
            
            prompt = f"""Please explain why the metric '{metric_name}' (ID: {metric_id})  {direction} from {comparison_mean} to {recent_mean} ({percent_change_str}) between {previous_month} and {recent_month} for district {district}.

Use the available tools to research this change and provide a comprehensive explanation that can be included in a monthly newsletter for city residents.
You should first look to supervisor district and see if there are localized trends in a particular neighborhood.  If so, you should include that in your explanation. 
Other than thay, prefer to share anomalies or datapoints that explain a large portion of the difference in the metric.  
If the anomaly relates to business location openings or closing, user set_dataset to get the dba_name of the businesses and share some of those.

Your response MUST follow this structured format:

EXPLANATION: [Your clear explanation of what happened]

DO NOT include any additional content, headers, or formatting outside of this structure. The explanation will be extracted directly for use in a report."""
            
            logger.info(f"Prompt for explainer agent: {prompt[:200]}...")
            
            # Add prompt to the session
            session_data = sessions[session_id]
            session_data["messages"].append({"role": "user", "content": prompt})
            
            # Process with the agent
            try:
                logger.info(f"Running explainer agent for metric: {metric_id}")
                # Run the agent in a non-streaming mode to get the complete response
                response = swarm_client.run(
                    agent=anomaly_explainer_agent,
                    messages=session_data["messages"],
                    context_variables=session_data["context_variables"],
                    stream=False
                )
                
                # Log information about the response type and attributes
                logger.info(f"Explainer agent response type: {type(response)}")
                if hasattr(response, '__dict__'):
                    logger.info(f"Response attributes: {list(response.__dict__.keys())}")
                elif isinstance(response, dict):
                    logger.info(f"Response keys: {list(response.keys())}")
                
                # Log the raw response for debugging
                try:
                    if hasattr(response, 'model_dump'):
                        logger.info(f"Response model_dump: {json.dumps(response.model_dump())[:500]}...")
                    elif isinstance(response, dict):
                        logger.info(f"Response dict: {json.dumps(response)[:500]}...")
                    elif hasattr(response, '__dict__'):
                        logger.info(f"Response __dict__: {json.dumps(response.__dict__)[:500]}...")
                    else:
                        logger.info(f"Response string: {str(response)[:500]}...")
                except Exception as dump_error:
                    logger.error(f"Error dumping response for logging: {str(dump_error)}")
                
                explanation = ""
                chart_data = None
                
                # Check if response contains content
                if response:
                    # Try to extract explanation from the response messages
                    try:
                        # Check if response has a 'messages' attribute
                        if hasattr(response, 'messages') and response.messages:
                            logger.info(f"Response has messages attribute with {len(response.messages)} messages")
                            # Get the last message
                            last_message = response.messages[-1]
                            logger.info(f"Last message type: {type(last_message)}")
                            
                            # Check if the message has content
                            if hasattr(last_message, 'content') and last_message.content:
                                explanation = str(last_message.content)
                                logger.info(f"Found explanation in last message content: {explanation[:100]}...")
                            # Check if the message has text
                            elif hasattr(last_message, 'text') and last_message.text:
                                explanation = str(last_message.text)
                                logger.info(f"Found explanation in last message text: {explanation[:100]}...")
                            # Check if the message is a dict with content
                            elif isinstance(last_message, dict) and 'content' in last_message:
                                explanation = str(last_message['content'])
                                logger.info(f"Found explanation in last message dict content: {explanation[:100]}...")
                            # Check if the message is a dict with text
                            elif isinstance(last_message, dict) and 'text' in last_message:
                                explanation = str(last_message['text'])
                                logger.info(f"Found explanation in last message dict text: {explanation[:100]}...")
                        
                        # If no explanation found in messages, try to extract from the response itself
                        if not explanation:
                            # If response is a string, try to parse it as JSON
                            if isinstance(response, str):
                                logger.info("Response is a string, attempting to parse as JSON")
                                try:
                                    response_json = json.loads(response)
                                    logger.info(f"Successfully parsed response as JSON: {json.dumps(response_json)[:200]}...")
                                    
                                    # Check if the JSON has a 'messages' field
                                    if 'messages' in response_json and response_json['messages']:
                                        messages = response_json['messages']
                                        if isinstance(messages, list) and len(messages) > 0:
                                            # Look at the last message
                                            last_message = messages[-1]
                                            if isinstance(last_message, dict) and 'content' in last_message:
                                                explanation = str(last_message['content'])
                                                logger.info(f"Found explanation in JSON messages[].content: {explanation[:100]}...")
                                            elif isinstance(last_message, dict) and 'text' in last_message:
                                                explanation = str(last_message['text'])
                                                logger.info(f"Found explanation in JSON messages[].text: {explanation[:100]}...")
                                except json.JSONDecodeError:
                                    logger.warning("Failed to parse response string as JSON")
                            
                            # If response has a model_dump method (Pydantic model)
                            elif hasattr(response, 'model_dump'):
                                logger.info("Response has model_dump method, using it")
                                response_json = response.model_dump()
                                logger.info(f"Response model_dump: {json.dumps(response_json)[:200]}...")
                                
                                # Check if the model_dump has a 'messages' field
                                if 'messages' in response_json and response_json['messages']:
                                    messages = response_json['messages']
                                    if isinstance(messages, list) and len(messages) > 0:
                                        # Look at the last message
                                        last_message = messages[-1]
                                        if isinstance(last_message, dict) and 'content' in last_message:
                                            explanation = str(last_message['content'])
                                            logger.info(f"Found explanation in model_dump messages[].content: {explanation[:100]}...")
                                        elif isinstance(last_message, dict) and 'text' in last_message:
                                            explanation = str(last_message['text'])
                                            logger.info(f"Found explanation in model_dump messages[].text: {explanation[:100]}...")
                            
                            # If response has a __dict__ attribute
                            elif hasattr(response, '__dict__'):
                                logger.info("Response has __dict__ attribute, using it")
                                response_dict = response.__dict__
                                logger.info(f"Response __dict__ keys: {list(response_dict.keys())}")
                                
                                # Check if the __dict__ has a 'messages' field
                                if 'messages' in response_dict and response_dict['messages']:
                                    messages = response_dict['messages']
                                    if isinstance(messages, list) and len(messages) > 0:
                                        # Look at the last message
                                        last_message = messages[-1]
                                        if hasattr(last_message, 'content') and last_message.content:
                                            explanation = str(last_message.content)
                                            logger.info(f"Found explanation in __dict__ messages[].content: {explanation[:100]}...")
                                        elif hasattr(last_message, 'text') and last_message.text:
                                            explanation = str(last_message.text)
                                            logger.info(f"Found explanation in __dict__ messages[].text: {explanation[:100]}...")
                                        elif isinstance(last_message, dict) and 'content' in last_message:
                                            explanation = str(last_message['content'])
                                            logger.info(f"Found explanation in __dict__ messages[] dict content: {explanation[:100]}...")
                                        elif isinstance(last_message, dict) and 'text' in last_message:
                                            explanation = str(last_message['text'])
                                            logger.info(f"Found explanation in __dict__ messages[] dict text: {explanation[:100]}...")
                            
                            # If response is already a dict
                            elif isinstance(response, dict):
                                logger.info("Response is already a dict")
                                
                                # Check if the dict has a 'messages' field
                                if 'messages' in response and response['messages']:
                                    messages = response['messages']
                                    if isinstance(messages, list) and len(messages) > 0:
                                        # Look at the last message
                                        last_message = messages[-1]
                                        if isinstance(last_message, dict) and 'content' in last_message:
                                            explanation = str(last_message['content'])
                                            logger.info(f"Found explanation in dict messages[].content: {explanation[:100]}...")
                                        elif isinstance(last_message, dict) and 'text' in last_message:
                                            explanation = str(last_message['text'])
                                            logger.info(f"Found explanation in dict messages[].text: {explanation[:100]}...")
                    except Exception as e:
                        logger.error(f"Error extracting explanation from response messages: {e}")
                        logger.error(f"Problematic response: {str(response)[:200]}...")
                        logger.error(traceback.format_exc())
                    
                    # If we still don't have an explanation, try to find chart data
                    if not explanation:
                        # Look for chart data in the response
                        try:
                            # Check if response has a 'chart_data' attribute
                            if hasattr(response, 'chart_data') and response.chart_data:
                                chart_data = response.chart_data
                                logger.info(f"Found chart_data in response attribute: {json.dumps(chart_data)[:100]}...")
                            
                            # Check if response has a 'chart_html' attribute
                            elif hasattr(response, 'chart_html') and response.chart_html:
                                chart_html = response.chart_html
                                logger.info(f"Found chart_html in response attribute: {str(chart_html)[:100]}...")
                                chart_data = {"html": str(chart_html)}
                            
                            # Check if response has tool_calls with chart data
                            elif hasattr(response, 'tool_calls') and response.tool_calls:
                                for tool_call in response.tool_calls:
                                    if isinstance(tool_call, dict):
                                        if 'chart' in str(tool_call).lower():
                                            logger.info(f"Found chart data in tool_calls dict")
                                            chart_data = tool_call
                                            break
                                    else:
                                        if hasattr(tool_call, 'function') and 'chart' in str(tool_call.function).lower():
                                            logger.info(f"Found chart data in tool_calls object")
                                            chart_data = tool_call
                                            break
                        except Exception as e:
                            logger.error(f"Error extracting chart data: {e}")
                            logger.error(f"Chart data extraction error traceback: {traceback.format_exc()}")
                
                # Validate the explanation
                if explanation:
                    if len(explanation) < 10:  # If explanation is too short
                        logger.warning(f"Extracted explanation is too short: {explanation}")
                        explanation = ""
                    elif len(explanation) > 5000:  # If explanation is too long
                        logger.warning(f"Extracted explanation is too long ({len(explanation)} chars), truncating")
                        explanation = explanation[:5000] + "..."
                
                # If explanation is still empty after all attempts, use a fallback
                if not explanation:
                    logger.warning(f"No explanation returned from agent for {metric_name}")
                    
                    # First try to use the explanation already in the database
                    if item.get("explanation"):
                        explanation = item.get("explanation")
                        logger.info(f"Using existing explanation from prioritization step: {explanation[:100]}...")
                    else:
                        # If there's no explanation at all, create a generic one
                        explanation = f"Unable to generate an automated explanation for the change in {metric_name} ({percent_change_str}) between {previous_month} and {recent_month}."
                
            except Exception as e:
                logger.error(f"Error running explainer agent: {str(e)}", exc_info=True)
                logger.error(f"Traceback: {traceback.format_exc()}")
                explanation = f"Error generating explanation for {metric_name}: {str(e)}"
                
            # Log the final explanation that will be stored
            logger.info(f"Final explanation to be stored for {metric_name} (length: {len(explanation)}): {explanation[:200]}...")
            
            # Prepare chart_html for storage in the database
            chart_html = None
            if chart_data:
                try:
                    # Extract chart HTML if it exists
                    if hasattr(chart_data, 'content'):
                        chart_html = chart_data.content
                    elif isinstance(chart_data, dict) and 'content' in chart_data:
                        chart_html = chart_data['content']
                    elif isinstance(chart_data, str) and ('<div' in chart_data or '<svg' in chart_data):
                        chart_html = chart_data
                    
                    if chart_html:
                        logger.info(f"Extracted chart HTML (first 100 chars): {str(chart_html)[:100]}...")
                except Exception as e:
                    logger.error(f"Error extracting chart HTML: {e}")
            
            # Update the database with the detailed explanation and chart data
            if explanation:  # Only update if we have a valid explanation
                try:
                    if chart_html:
                        cursor.execute("""
                            UPDATE monthly_reporting
                            SET report_text = %s, chart_data = %s
                            WHERE id = %s
                        """, (explanation, json.dumps({"html": str(chart_html)}), report_id))
                        logger.info(f"Updated explanation and chart data for report ID {report_id}")
                    else:
                        cursor.execute("""
                            UPDATE monthly_reporting
                            SET report_text = %s
                            WHERE id = %s
                        """, (explanation, report_id))
                        logger.info(f"Updated explanation for report ID {report_id}")
                    
                    successful_explanations += 1
                except Exception as db_error:
                    logger.error(f"Database error while updating explanation: {str(db_error)}")
                    logger.error(traceback.format_exc())
            else:
                logger.warning(f"No valid explanation to store for report ID {report_id}")
        
        connection.commit()
        cursor.close()
        
        return successful_explanations
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=generate_explanations_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] == "success":
        logger.info(f"Successfully generated explanations for {result['result']} items")
        return {"status": "success", "message": f"Explanations generated for {result['result']} items"}
    else:
        return {"status": "error", "message": result["message"]}

def generate_monthly_report(report_date=None, district="0"):
    """
    Step 4: Generate the final monthly report with charts and annotations
    
    Args:
        report_date: The date for the report (defaults to current month)
        district: District number
        
    Returns:
        Path to the generated report file
    """
    from webChat import client, AGENT_MODEL
    
    logger.info(f"Generating monthly report for district {district}")
    
    # Set report date to current date if not provided
    if not report_date:
        report_date = datetime.now().date()
    
    def generate_report_operation(connection):
        cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get all report items for this date and district, ordered by priority
        cursor.execute("""
            SELECT *, 
                   metadata->>'anomaly_id' as anomaly_id, -- Extract potential anomaly_id from metadata
                   metadata->>'perplexity_context' as perplexity_context,
                   metadata->'perplexity_response' as perplexity_response_json,
                   metadata->'perplexity_response'->'citations' as perplexity_citations,
                   metric_id -- Placeholder for metric_id, needs proper retrieval logic
            FROM monthly_reporting 
            WHERE report_date = %s AND district = %s
            ORDER BY priority
        """, (report_date, district))
        
        items = cursor.fetchall()
        logger.info(f"Found {len(items)} report items for date {report_date} and district {district}")
        if not items:
            logger.warning(f"No report items found for date {report_date} and district {district}")
            cursor.close()
            return None
        
        # Format the report data
        report_data = []
        for item in items:
            # Check if there's chart data
            chart_html = None
            if item["chart_data"]:
                try:
                    chart_data = item["chart_data"]
                    if isinstance(chart_data, dict) and "html" in chart_data:
                        chart_html = chart_data["html"]
                    elif isinstance(chart_data, str):
                        chart_data_dict = json.loads(chart_data)
                        if "html" in chart_data_dict:
                            chart_html = chart_data_dict["html"]
                except Exception as e:
                    logger.error(f"Error parsing chart data: {e}")
            
            # Attempt to get IDs for chart placeholders
            metric_id = item.get("metric_id") # Placeholder value for now
            anomaly_id = item.get("anomaly_id") # Might be None if not an anomaly item
            
            # Decide which chart placeholder format to use
            chart_placeholder = ""
            if anomaly_id:
                try:
                    # Validate anomaly_id is an integer
                    int(anomaly_id) 
                    chart_placeholder = f"[CHART:anomaly:{anomaly_id}]"
                except (ValueError, TypeError):
                    anomaly_id = None # Invalid anomaly_id
            
            # Fallback to time series if no valid anomaly_id or if metric_id is preferred
            if not anomaly_id and metric_id is not None:
                try:
                    # Validate metric_id, district, period_type
                    int(metric_id)
                    str(item["district"])
                    str(item["period_type"])
                    chart_placeholder = f"[CHART:time_series:{metric_id}:{item['district']}:{item['period_type']}]"
                except (ValueError, TypeError, KeyError):
                    metric_id = None # Invalid data
                    
            # Format Perplexity citations if available
            perplexity_citations_text = ""
            citation_map = {}  # Map to store citation numbers for reference matching
            
            # Direct extraction of citations from the database column
            # This is the most reliable way to get the citations
            perplexity_citations = item.get("perplexity_citations")
            logger.info(f"Raw perplexity_citations for {item['metric_name']}: {json.dumps(perplexity_citations)[:300]}...")
            
            # Try to format the citations
            if perplexity_citations:
                try:
                    # Handle string format (needs parsing)
                    if isinstance(perplexity_citations, str):
                        try:
                            perplexity_citations = json.loads(perplexity_citations)
                        except json.JSONDecodeError:
                            logger.error(f"Failed to parse perplexity_citations as JSON: {perplexity_citations}")
                    
                    # Handle list format
                    if isinstance(perplexity_citations, list) and len(perplexity_citations) > 0:
                        perplexity_citations_text = "\n\nRelevant Sources:\n"
                        for idx, citation in enumerate(perplexity_citations, 1):
                            if isinstance(citation, str):
                                # Simple URL citation
                                citation_map[str(idx)] = citation
                                perplexity_citations_text += f"{idx}. {citation}\n"
                            elif isinstance(citation, dict):
                                # Citation with title and URL
                                title = citation.get("title", "Untitled")
                                url = citation.get("url", citation.get("link", "No URL"))
                                citation_map[str(idx)] = {"title": title, "url": url}
                                perplexity_citations_text += f"{idx}. {title}: {url}\n"
                        
                        logger.info(f"Formatted {len(perplexity_citations)} citations for {item['metric_name']}")
                        logger.info(f"Citation map: {json.dumps(citation_map)}")
                except Exception as e:
                    logger.error(f"Error formatting citations: {e}")
                    logger.error(f"Problematic citations data: {perplexity_citations}")
            
            # If no citations found yet, try a fallback approach
            if not perplexity_citations_text:
                logger.warning(f"No citations found for {item['metric_name']} in primary method, trying fallback approaches")
                
                # Fallback 1: Try to get the full perplexity_response and extract citations
                try:
                    perplexity_response_json = item.get("perplexity_response_json")
                    if perplexity_response_json:
                        # Handle string format (needs parsing)
                        if isinstance(perplexity_response_json, str):
                            try:
                                perplexity_response_json = json.loads(perplexity_response_json)
                            except json.JSONDecodeError:
                                logger.error(f"Failed to parse perplexity_response_json as JSON")
                        
                        # Extract citations from the response JSON
                        if isinstance(perplexity_response_json, dict) and "citations" in perplexity_response_json:
                            citations = perplexity_response_json["citations"]
                            if isinstance(citations, list) and len(citations) > 0:
                                perplexity_citations_text = "\n\nRelevant Sources:\n"
                                for idx, citation in enumerate(citations, 1):
                                    if isinstance(citation, str):
                                        citation_map[str(idx)] = citation
                                        perplexity_citations_text += f"{idx}. {citation}\n"
                                    elif isinstance(citation, dict):
                                        title = citation.get("title", "Untitled")
                                        url = citation.get("url", citation.get("link", "No URL"))
                                        citation_map[str(idx)] = {"title": title, "url": url}
                                        perplexity_citations_text += f"{idx}. {title}: {url}\n"
                                
                                logger.info(f"Fallback: Extracted {len(citations)} citations from perplexity_response_json")
                except Exception as e:
                    logger.error(f"Error in citation fallback approach: {e}")
            
            # Process the perplexity context to match citation references
            perplexity_context = item.get("perplexity_context", "")
            processed_context = perplexity_context
            
            # Only process if we have both citations and context
            if citation_map and perplexity_context:
                # Look for citation references like [1], [2], etc.
                try:
                    # Find all citation references in the format [n]
                    citation_pattern = r'\[(\d+)\]'
                    citation_refs = re.findall(citation_pattern, perplexity_context)
                    
                    # Sort and deduplicate the citation references
                    unique_refs = sorted(set(citation_refs), key=int)
                    
                    logger.info(f"Found citation references in context: {unique_refs}")
                    
                    # Replace each citation reference with a hyperlink if possible
                    for ref in unique_refs:
                        if ref in citation_map:
                            citation = citation_map[ref]
                            if isinstance(citation, dict):
                                url = citation.get("url", "")
                                if url:
                                    # Replace [n] with [n](url)
                                    processed_context = processed_context.replace(
                                        f"[{ref}]", 
                                        f"[{ref}]({url})"
                                    )
                            elif isinstance(citation, str) and citation.startswith("http"):
                                # If citation is just a URL string
                                processed_context = processed_context.replace(
                                    f"[{ref}]", 
                                    f"[{ref}]({citation})"
                                )
                except Exception as e:
                    logger.error(f"Error processing citation references: {e}")
                    # Keep the original context if there's an error

            report_data.append({
                "metric": item["metric_name"],
                "metric_id": item["metric_id"],
                "group": item["group_value"],
                "recent_mean": item["recent_mean"],
                "comparison_mean": item["comparison_mean"],
                "difference": item["difference"],
                "percent_change": item["percent_change"],
                "explanation": item["explanation"],
                "report_text": item["report_text"],
                "priority": item["priority"],
                "chart_html": chart_html, # Keep original chart_html if already generated
                "chart_placeholder": chart_placeholder, # Add the placeholder string
                "trend_analysis": item["metadata"].get("trend_analysis", "") if item["metadata"] else "",
                "follow_up": item["metadata"].get("follow_up", "") if item["metadata"] else "",
                "perplexity_context": processed_context,  # Use the processed context with linked citations
                "perplexity_citations_text": perplexity_citations_text,
                "citation_map": citation_map,  # Include the citation map for reference
            })
            
            # Only add perplexity_response if it exists
            if 'perplexity_response_json' in locals():
                report_data[-1]["perplexity_response"] = perplexity_response_json
        
        cursor.close()
        
        # Get district name for the report title
        district_name = f"District {district}"
        if district == "0":
            district_name = "Citywide"
            
        # Load officials data
        officials_data = {}
        try:
            officials_path = Path(__file__).parent / 'data' / 'dashboard' / 'officials.json'
            with open(officials_path, 'r') as f:
                officials_json = json.load(f)
                for official in officials_json.get("officials", []):
                    officials_data[official.get("district")] = official
        except Exception as e:
            logger.error(f"Error loading officials data: {e}")
            
        # Get the district official
        district_official = officials_data.get(district, {})
        official_name = district_official.get("name", "")
        official_role = district_official.get("role", "")
        
        # Create a prompt for generating the report
        report_items_text = ""
        for i, item in enumerate(report_data, 1):
            report_items_text += f"""
ITEM {i}: {item['metric']} - {item['group']}
Change: {item['recent_mean']:.2f} vs {item['comparison_mean']:.2f} ({item['percent_change']:+.2f}%)
Summary: {item['explanation']}
Trend Analysis: {item['trend_analysis']}
Follow-up Questions: {item['follow_up']}
Potential Chart: {item['chart_placeholder'] if item['chart_placeholder'] else 'No'}

Detailed Analysis:
{item['report_text']}

"""
            # Add Perplexity context if available
            if item['perplexity_context']:
                report_items_text += f"Additional Context from Perplexity:\n{item['perplexity_context']}\n\n"

            # Add Perplexity citations if available
            if item['perplexity_citations_text']:
                report_items_text += f"{item['perplexity_citations_text']}\n"
                
            if item['chart_html']:
                report_items_text += f"Chart Available: Yes\n"
        
        # Calculate the month for the report title (the more recent month in the comparison)
        if report_date.month == 1:  # January case
            report_month_date = report_date.replace(month=12, year=report_date.year-1)
        else:
            report_month_date = report_date.replace(month=report_date.month-1)
            
        # Format the month name for the report title
        current_month = report_month_date.strftime("%B %Y")
        
        # Load prompt from JSON file
        try:
            prompts = load_prompts()
            prompt_template = prompts['monthly_report']['generate_report']['prompt']
            system_message = prompts['monthly_report']['generate_report']['system']
            
            # Format the prompt with the required variables
            prompt = prompt_template.format(
                district_name=district_name,
                current_month=current_month,
                official_name=official_name,
                official_role=official_role,
                report_items_text=report_items_text
            )
        except Exception as e:
            logger.error(f"Error loading prompts from JSON: {e}")
            raise

        # Make API call to generate the report
        logger.info("Making API call to generate report content")
        response = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[{"role": "system", "content": system_message},
                     {"role": "user", "content": prompt}],
            temperature=0.2
        )

        report_text = response.choices[0].message.content
        logger.info(f"Received report content (length: {len(report_text)})")
        
        # Once we have the report text, insert any charts if they exist
        for i, item in enumerate(report_data, 1):
            if item.get('chart_html'):
                # Create the chart HTML content we want to insert
                chart_content_to_insert = item['chart_html']
                
                # Look for a section that mentions this metric to insert the chart nearby
                metric_mentions = [
                    item['metric'],
                    # Add variations if needed (e.g., removing emojis)
                    item['metric'].replace("🏠", "Housing").replace("💊", "Drug").replace("🚫", "Business") 
                ]
                
                insertion_point = -1
                for mention in metric_mentions:
                    mention_pos = report_text.find(mention)
                    if mention_pos != -1:
                        # Find the end of the paragraph containing the mention
                        paragraph_end = report_text.find("\\n\\n", mention_pos)
                        if paragraph_end != -1:
                            insertion_point = paragraph_end + 2 # Insert after the double newline
                            break
                        else:
                            # If no double newline found, try finding the end of the line
                            line_end = report_text.find("\\n", mention_pos)
                            if line_end != -1:
                                insertion_point = line_end + 1 # Insert after the newline
                                break
                            else:
                                # If no newline found, just insert after the mention (less ideal)
                                insertion_point = mention_pos + len(mention)
                                break

                if insertion_point != -1:
                    # Insert the chart HTML at the determined point
                    report_text = report_text[:insertion_point] + f"\\n{chart_content_to_insert}\\n\\n" + report_text[insertion_point:]
                    logger.info(f"Inserted chart for '{item['metric']}' after its mention.")
                else:
                    # If we couldn't find a suitable mention, append the chart at the end
                    report_text += f"\\n\\n{chart_content_to_insert}\\n"
                    logger.info(f"Appended chart for '{item['metric']}' at the end of the report.")
        
        # Create directory for reports if it doesn't exist
        reports_dir = Path(__file__).parent / 'output' / 'reports'
        reports_dir.mkdir(parents=True, exist_ok=True)
        
        # Save the report to a file as HTML
        report_filename = f"monthly_report_{district}_{report_date.strftime('%Y_%m')}.html"
        report_path = reports_dir / report_filename
        
        # Add proper HTML structure with modern styling
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{district_name} Monthly Newsletter - {current_month}</title>
    <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500&display=swap">
    <style>
        :root {{
            --ink-black: #000000;
            --dark-gray: #333333;
            --medium-gray: #666666;
            --light-gray: #E8E9EB;
            --white: #ffffff;
            --text-color: #222222;
            --border-color: #CCCCCC;
            --accent-color: #444444;
        }}
        
        body {{
            font-family: 'IBM Plex Sans', Arial, Helvetica Neue, sans-serif;
            line-height: 1.6;
            color: var(--text-color);
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: var(--white);
        }}
        
       
        
        /* Typography */
        h1 {{
            font-family: 'Inter', Arial, Helvetica Neue, sans-serif;
            font-weight: 700;
            font-size: 36px;
            line-height: 42px;
            color: var(--ink-black);
            margin-top: 0;
            margin-bottom: 20px;
        }}
        
        h2 {{
            font-family: 'Inter', Arial, Helvetica Neue, sans-serif;
            font-weight: 500;
            font-size: 24px;
            line-height: 32px;
            color: var(--ink-black);
            margin-top: 30px;
            margin-bottom: 15px;
        }}
        
        h3 {{
            font-family: 'Inter', Arial, Helvetica Neue, sans-serif;
            font-weight: 400;
            font-size: 18px;
            line-height: 28px;
            color: var(--ink-black);
            margin-top: 25px;
            margin-bottom: 10px;
        }}
        
        p {{
            font-family: 'IBM Plex Sans', Arial, Helvetica Neue, sans-serif;
            font-size: 14px;
            line-height: 22px;
            margin-bottom: 15px;
        }}
        
        .caption {{
            font-family: 'IBM Plex Sans', Arial, Helvetica Neue, sans-serif;
            font-size: 12px;
            line-height: 18px;
            color: var(--medium-gray);
            margin-top: 5px;
            margin-bottom: 20px;
        }}
        
        /* Content sections */
        .section {{
            margin-bottom: 30px;
            padding: 20px;
            background-color: var(--light-gray);
            border-radius: 8px;
        }}
        
        .highlight {{
            background-color: var(--light-gray);
            padding: 15px;
            border-radius: 6px;
            margin: 20px 0;
            border-left: 4px solid var(--ink-black);
        }}
        
        .key-takeaways {{
            background-color: var(--white);
            padding: 15px;
            border-radius: 6px;
            margin: 20px 0;
            border-left: 4px solid #007bff;
        }}
        
        .data-point {{
            color: var(--ink-black);
            font-weight: 500;
        }}
        
        .call-to-action {{
            background-color: var(--ink-black);
            color: var(--white);
            padding: 10px 15px;
            border-radius: 4px;
            display: inline-block;
            margin: 10px 0;
            text-decoration: none;
        }}
        
        .chart-container {{
            margin: 20px auto;
            padding: 15px;
            border-radius: 6px;
            max-width: 600px;
            background-color: var(--white); /* ADDED for consistency */
        }}
        
        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid var(--border-color);
            font-size: 12px;
            color: var(--medium-gray);
        }}
    </style>
</head>
<body>
    {report_text}
    <div class="footer">
        <p>Generated on {datetime.now().strftime('%B %d, %Y')} by TransparentSF</p>
    </div>
</body>
</html>
"""
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"Monthly newsletter saved to {report_path}")
        return str(report_path)
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=generate_report_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] == "success" and result["result"]:
        return {"status": "success", "report_path": result["result"]}
    elif result["status"] == "success" and not result["result"]:
        return {"status": "error", "message": "No report items found"}
    else:
        return {"status": "error", "message": result["message"]}

def proofread_and_revise_report(report_path):
    """
    Step 5: Proofread and revise the monthly newsletter
    
    Args:
        report_path: Path to the newsletter file to proofread
        
    Returns:
        Path to the revised newsletter file
    """
    from webChat import client, AGENT_MODEL
    
    logger.info(f"Proofreading and revising newsletter at {report_path}")
    
    try:
        # Read the newsletter
        with open(report_path, 'r', encoding='utf-8') as f:
            newsletter_text = f.read()
        
        # Load prompt from JSON file
        prompts = load_prompts()
        prompt_template = prompts['monthly_report']['proofread']['prompt']
        system_message = prompts['monthly_report']['proofread']['system']
        
        # Format the prompt with the newsletter text
        prompt = prompt_template.format(
            newsletter_text=newsletter_text
        )

        # Use the AGENT_MODEL directly
        logger.info("Using AGENT_MODEL for proofreading")
        response = client.chat.completions.create(
            model=AGENT_MODEL,
            messages=[{"role": "system", "content": system_message},
                     {"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"}  # Expect JSON response
        )
        response_content = response.choices[0].message.content
        
        # Parse the JSON response
        try:
            proofread_data = json.loads(response_content)
            revised_newsletter_content = proofread_data.get("newsletter")
            proofread_feedback = proofread_data.get("proofread_feedback")
            headlines = proofread_data.get("headlines") # Extract headlines
            
            if not revised_newsletter_content:
                logger.error("No 'newsletter' content found in proofread response.")
                return {"status": "error", "message": "Proofread response missing 'newsletter' content."}

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse proofread response as JSON: {e}")
            logger.error(f"Raw response: {response_content[:500]}...")
            return {"status": "error", "message": f"Failed to parse proofread JSON response: {e}"}
        
        # Save the revised newsletter content
        report_path_obj = Path(report_path)
        revised_filename = f"{report_path_obj.stem}_revised{report_path_obj.suffix}"
        revised_path = report_path_obj.parent / revised_filename
        
        with open(revised_path, 'w', encoding='utf-8') as f:
            f.write(revised_newsletter_content)
        
        # Expand chart references in the revised report
        logger.info(f"Expanding chart references in revised report: {revised_path}")
        expand_result = expand_chart_references(revised_path)
        if not expand_result:
            logger.warning("Failed to expand chart references in the revised report")

        # Update the reports table with the revised filename and proofread_feedback
        def update_report_record_operation(connection):
            cursor = connection.cursor()
            original_filename = report_path_obj.name # Assuming original filename is the name part of report_path
            
            # Find the report_id based on the original filename
            # This assumes original_filename in the reports table matches report_path_obj.name
            cursor.execute("""
                SELECT id FROM reports WHERE original_filename = %s ORDER BY created_at DESC LIMIT 1
            """, (original_filename,))
            report_record = cursor.fetchone()
            
            if not report_record:
                logger.error(f"Could not find report record for original_filename: {original_filename}")
                return False
            
            report_id = report_record[0]
            
            update_query = """
                UPDATE reports
                SET revised_filename = %s, proofread_feedback = %s, headlines = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """
            cursor.execute(update_query, (revised_filename, proofread_feedback, json.dumps(headlines) if headlines else None, report_id))
            connection.commit()
            logger.info(f"Updated report ID {report_id} with revised filename, proofread feedback, and headlines.")
            return True

        update_db_result = execute_with_connection(
            operation=update_report_record_operation,
            db_host=DB_HOST,
            db_port=DB_PORT,
            db_name=DB_NAME,
            db_user=DB_USER,
            db_password=DB_PASSWORD
        )

        if update_db_result["status"] != "success" or not update_db_result["result"]:
            logger.error(f"Failed to update report table: {update_db_result.get('message')}")
            # Continue, but log the error. The file is saved.

        logger.info(f"Revised newsletter saved to {revised_path}")
        return {"status": "success", "revised_report_path": str(revised_path)}
        
    except Exception as e:
        error_msg = f"Error in proofread_and_revise_report: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def get_perplexity_context(report_items):
    """
    Use the Perplexity API to get additional context and information about 
    each individual item in the report. Enriches each item with relevant real-time information.
    
    Args:
        report_items: List of report items with metric information
        
    Returns:
        Dictionary with status and the enriched report items with context
    """
    logger.info(f"Getting additional context from Perplexity API for {len(report_items)} report items")
    
    # Check if Perplexity API key is available
    if not PERPLEXITY_API_KEY:
        logger.warning("No Perplexity API key available. Skipping context enrichment.")
        return {"status": "error", "message": "No Perplexity API key available"}
    
    try:
        # Load prompt from prompts.json
        prompts = load_prompts()
        prompt_template = prompts['monthly_report']['context_enrichment']['prompt']
        system_message = prompts['monthly_report']['context_enrichment']['system']
        
        # Process each item individually
        for item in report_items:
            metric_name = item.get("metric")
            group_value = item.get("group")
            recent_mean = item.get("recent_mean")
            comparison_mean = item.get("comparison_mean")
            percent_change = item.get("percent_change")
            explanation = item.get("explanation", "")
            report_text = item.get("report_text", "")
            
            # Prepare item-specific context for the prompt
            item_context = f"""
Metric: {metric_name}
Group: {group_value}
Recent Value: {recent_mean}
Previous Value: {comparison_mean}
Percent Change: {percent_change:+.2f}%
Explanation: {explanation}
Detailed Analysis: {report_text}
"""

            # Format the prompt with the item context
            prompt = prompt_template.format(
                text_content=item_context
            )
            
            # log the prompt
            logger.info(f"Prompt for Perplexity API:\nSystem: {system_message}\nUser: {prompt}")
            
            # Make the API call to Perplexity
            url = "https://api.perplexity.ai/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {PERPLEXITY_API_KEY}"
            }
            
            payload = {
                "model": "sonar",
                "messages": [
                    {
                        "role": "system",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }
            
            # Make the API request
            logger.info(f"Sending request to Perplexity API for {metric_name}")
            response = requests.post(url, json=payload, headers=headers)
            
            # Check if the request was successful
            if response.status_code == 200:
                logger.info(f"Successfully received response from Perplexity API for {metric_name}")
                result = response.json()
                
                # Log the raw response for debugging
                logger.info(f"Raw Perplexity response: {json.dumps(result)[:1000]}...")
                
                # Extract the content from the response
                context_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                if not context_content:
                    logger.warning(f"Received empty content from Perplexity API for {metric_name}")
                    continue
                
                # Initialize metadata if it doesn't exist
                if not item.get("metadata"):
                    item["metadata"] = {}
                
                # Store the context content
                item["metadata"]["perplexity_context"] = context_content
                
                # Store the complete response for citations and other data
                # Extract the relevant parts from the response to avoid storing unnecessary data
                perplexity_response = {}
                
                # FIXED: Extract citations from the top level of the response (not from message)
                if "citations" in result:
                    perplexity_response["citations"] = result["citations"]
                    logger.info(f"Found top-level citations: {json.dumps(result['citations'])}")
                else:
                    logger.info("No top-level citations found in Perplexity response")
                    
                    # Fallback: Check if citations are in the message
                    if "choices" in result and len(result["choices"]) > 0:
                        message = result["choices"][0].get("message", {})
                        logger.info(f"Message from Perplexity: {json.dumps(message)[:1000]}...")
                        
                        if "citations" in message:
                            perplexity_response["citations"] = message["citations"]
                            logger.info(f"Found message-level citations: {json.dumps(message['citations'])}")
                        else:
                            logger.info("No message-level citations found in Perplexity response")
                
                # Store other relevant fields from the top level
                for field in ["links", "search_queries", "attachments", "tool_calls"]:
                    if field in result:
                        perplexity_response[field] = result[field]
                        logger.info(f"Found top-level {field} in Perplexity response")
                
                # Save the full response data
                if perplexity_response:
                    item["metadata"]["perplexity_response"] = perplexity_response
                    logger.info(f"Added Perplexity response data to {metric_name}: {json.dumps(perplexity_response)}")
                else:
                    logger.warning(f"No perplexity_response data found for {metric_name}")
                
                logger.info(f"Added Perplexity context to {metric_name} (length: {len(context_content)})")
            else:
                logger.error(f"Perplexity API error for {metric_name}: {response.status_code} - {response.text}")
        
        logger.info("Successfully retrieved additional context from Perplexity API for all items")
        return {
            "status": "success", 
            "report_items": report_items
        }
        
    except Exception as e:
        error_msg = f"Error in get_perplexity_context: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def run_monthly_report_process(district="0", period_type="month", max_report_items=10):
    """
    Run the complete monthly newsletter generation process
    
    Args:
        district: District number (0 for citywide)
        period_type: Time period type (month, quarter, year)
        max_report_items: Maximum number of items to include in the newsletter
        
    Returns:
        Status dictionary with newsletter path
    """
    logger.info(f"Starting monthly newsletter process for district {district}")
    
    try:
        # Step 0: Initialize the monthly_reporting table
        logger.info("Step 0: Initializing monthly_reporting table")
        init_result = initialize_monthly_reporting_table()
        if not init_result:
            return {"status": "error", "message": "Failed to initialize monthly_reporting table"}
        
        # Step 1: Select deltas to discuss
        logger.info("Step 1: Selecting deltas to discuss")
        deltas = select_deltas_to_discuss(period_type=period_type, district=district)
        if deltas.get("status") != "success":
            return deltas
            
        # Step 2: Prioritize deltas and get explanations
        logger.info("Step 2: Prioritizing deltas")
        prioritized = prioritize_deltas(deltas, max_items=max_report_items)
        if prioritized.get("status") != "success":
            return prioritized
        
        # Store prioritized items in the database
        logger.info("Step 2.5: Storing prioritized items")
        store_result = store_prioritized_items(
            prioritized.get("prioritized_items", []),
            period_type=period_type,
            district=district
        )
        if store_result.get("status") != "success":
            return store_result
            
        # Step 3: Generate detailed explanations
        logger.info("Step 3: Generating explanations")
        explanation_result = generate_explanations(store_result.get("inserted_ids", []))
        if explanation_result.get("status") != "success":
            return explanation_result
            
        # Get the report data for context enrichment
        def get_report_items_operation(connection):
            cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
            
            # Get all report items for this district, ordered by priority
            cursor.execute("""
                SELECT mr.*, r.id as report_id 
                FROM monthly_reporting mr
                JOIN reports r ON mr.report_id = r.id
                WHERE r.district = %s
                ORDER BY mr.priority
            """, (district,))
            
            items = cursor.fetchall()
            
            # Format the report data
            report_data = []
            for item in items:
                report_data.append({
                    "metric": item["metric_name"],
                    "metric_id": item["metric_id"],
                    "group": item["group_value"],
                    "recent_mean": item["recent_mean"],
                    "comparison_mean": item["comparison_mean"],
                    "difference": item["difference"],
                    "percent_change": item["percent_change"],
                    "explanation": item["explanation"],
                    "report_text": item["report_text"],
                    "priority": item["priority"],
                    "metadata": item["metadata"] if item["metadata"] else {}
                })
            
            cursor.close()
            return report_data
        
        # Step 4: Get report items for context enrichment
        logger.info("Step 4: Getting report items for Perplexity context enrichment")
        result = execute_with_connection(
            operation=get_report_items_operation,
            db_host=DB_HOST,
            db_port=DB_PORT,
            db_name=DB_NAME,
            db_user=DB_USER,
            db_password=DB_PASSWORD
        )
        
        if result["status"] != "success":
            logger.warning(f"Failed to get report items for context enrichment: {result['message']}")
            report_items = []
        else:
            report_items = result["result"]
            
        # Step 5: Get additional context for each report item
        if report_items:
            logger.info("Step 5: Getting additional context from Perplexity API for each report item")
            context_result = get_perplexity_context(report_items)
            
            if context_result.get("status") == "success":
                # Update the items in the database with the new context
                def update_items_with_context_operation(connection):
                    cursor = connection.cursor()
                    updated_count = 0
                    
                    for item in context_result.get("report_items", []):
                        metric_name = item["metric"]
                        group_value = item["group"]
                        
                        if "perplexity_context" in item.get("metadata", {}):
                            perplexity_context = item["metadata"]["perplexity_context"]
                            logger.info(f"Updating perplexity_context for {metric_name} - {group_value} (length: {len(perplexity_context)})")
                            
                            # Find the item in the database by metric_name and group_value
                            cursor.execute("""
                                UPDATE monthly_reporting
                                SET metadata = jsonb_set(
                                    CASE WHEN metadata IS NULL THEN '{}'::jsonb ELSE metadata END, 
                                    '{perplexity_context}', 
                                    %s::jsonb
                                )
                                WHERE metric_name = %s AND group_value = %s AND district = %s
                                RETURNING id
                            """, (
                                json.dumps(perplexity_context),
                                metric_name,
                                group_value,
                                district
                            ))
                            updated_ids = cursor.fetchall()
                            updated_count += len(updated_ids)
                            logger.info(f"Updated {len(updated_ids)} records for perplexity_context ({metric_name} - {group_value})")
                        
                        # Also update perplexity_response if available
                        if "perplexity_response" in item.get("metadata", {}):
                            perplexity_response = item["metadata"]["perplexity_response"]
                            logger.info(f"Updating perplexity_response for {metric_name} - {group_value}: {json.dumps(perplexity_response)[:100]}...")
                            
                            try:
                                # FIXED: Ensure perplexity_response is properly serialized to JSON as a string
                                # PostgreSQL jsonb_set expects a string that can be parsed as JSON
                                perplexity_response_json = json.dumps(perplexity_response)
                                
                                # Log the JSON string we're using
                                logger.info(f"Serialized perplexity_response JSON: {perplexity_response_json[:100]}...")
                                
                                # Here's the corrected SQL query:
                                # - We need to cast the JSON string to jsonb using ::jsonb
                                # - We're using the ? operator to check if a key exists in the JSONB
                                cursor.execute("""
                                    UPDATE monthly_reporting
                                    SET metadata = jsonb_set(
                                        CASE WHEN metadata IS NULL THEN '{}'::jsonb ELSE metadata END, 
                                        '{perplexity_response}', 
                                        %s::jsonb
                                    )
                                    WHERE metric_name = %s AND group_value = %s AND district = %s
                                    RETURNING id
                                """, (
                                    perplexity_response_json,
                                    metric_name,
                                    group_value,
                                    district
                                ))
                                response_updated_ids = cursor.fetchall()
                                logger.info(f"Updated {len(response_updated_ids)} records for perplexity_response ({metric_name} - {group_value})")
                                
                                # Verify this specific update worked
                                cursor.execute("""
                                    SELECT metadata->'perplexity_response' 
                                    FROM monthly_reporting 
                                    WHERE metric_name = %s AND group_value = %s AND district = %s
                                    LIMIT 1
                                """, (metric_name, group_value, district))
                                result = cursor.fetchone()
                                if result and result[0]:
                                    logger.info(f"Verification successful! perplexity_response was stored for {metric_name}: {json.dumps(result[0])[:100]}...")
                                else:
                                    logger.warning(f"Verification failed! perplexity_response was not stored for {metric_name}")
                            except Exception as e:
                                logger.error(f"Error updating perplexity_response: {str(e)}")
                                logger.error(f"Problematic perplexity_response: {perplexity_response}")
                                
                                # Try a direct update as a fallback (update the entire metadata)
                                try:
                                    logger.info("Attempting fallback update method for perplexity_response...")
                                    
                                    # First get the current metadata
                                    cursor.execute("""
                                        SELECT metadata FROM monthly_reporting
                                        WHERE metric_name = %s AND group_value = %s AND district = %s
                                        LIMIT 1
                                    """, (metric_name, group_value, district))
                                    
                                    current_metadata = cursor.fetchone()[0] or {}
                                    if not isinstance(current_metadata, dict):
                                        current_metadata = {}
                                    
                                    # Update the metadata with the new perplexity_response
                                    current_metadata['perplexity_response'] = perplexity_response
                                    
                                    # Update the entire metadata object
                                    cursor.execute("""
                                        UPDATE monthly_reporting
                                        SET metadata = %s::jsonb
                                        WHERE metric_name = %s AND group_value = %s AND district = %s
                                        RETURNING id
                                    """, (
                                        json.dumps(current_metadata),
                                        metric_name,
                                        group_value,
                                        district
                                    ))
                                    
                                    fallback_updated_ids = cursor.fetchall()
                                    logger.info(f"Fallback update successful for {len(fallback_updated_ids)} records")
                                except Exception as fallback_error:
                                    logger.error(f"Fallback update also failed: {str(fallback_error)}")
                    
                    # Verify the updates worked
                    cursor.execute("""
                        SELECT COUNT(*) FROM monthly_reporting 
                        WHERE district = %s AND metadata ? 'perplexity_context'
                    """, (district,))
                    context_count = cursor.fetchone()[0]
                    
                    cursor.execute("""
                        SELECT COUNT(*) FROM monthly_reporting 
                        WHERE district = %s AND metadata ? 'perplexity_response'
                    """, (district,))
                    response_count = cursor.fetchone()[0]
                    
                    logger.info(f"Verification counts: {context_count} records with perplexity_context, {response_count} with perplexity_response")
                    
                    connection.commit()
                    cursor.close()
                    return updated_count
                
                # Update the items in the database
                logger.info("Step 5.5: Updating database with Perplexity context")
                update_result = execute_with_connection(
                    operation=update_items_with_context_operation,
                    db_host=DB_HOST,
                    db_port=DB_PORT,
                    db_name=DB_NAME,
                    db_user=DB_USER,
                    db_password=DB_PASSWORD
                )
                
                if update_result["status"] == "success":
                    logger.info(f"Updated {update_result['result']} items with Perplexity context")
                else:
                    logger.warning(f"Failed to update items with Perplexity context: {update_result['message']}")
            else:
                logger.warning(f"Failed to get additional context: {context_result.get('message')}")
                # Don't fail the process if context retrieval fails
            
        # Step 6: Generate the monthly newsletter (with enriched context already in the database)
        logger.info("Step 6: Generating monthly newsletter")
        newsletter_result = generate_monthly_report(district=district)
        if newsletter_result.get("status") != "success":
            return newsletter_result
            
        # Step 7: Expand chart references in the newsletter
        logger.info("Step 7: Expanding chart references in the newsletter")
        expand_result = expand_chart_references(newsletter_result.get("report_path"))
        if not expand_result:
            logger.warning("Failed to expand chart references in the newsletter")

        # Step 8: Proofread and revise the newsletter
        logger.info("Step 8: Proofreading and revising newsletter")
        revised_result = proofread_and_revise_report(newsletter_result.get("report_path"))
        if revised_result.get("status") != "success":
            return revised_result
        
        # Use the revised newsletter path
        revised_newsletter_path = revised_result.get("revised_report_path")
        
        # Step 9: Generate an email-compatible version of the newsletter
        logger.info("Step 9: Generating email-compatible version of newsletter")
        email_result = None
        if revised_newsletter_path:
            email_result = generate_email_compatible_report(revised_newsletter_path)
            if email_result:
                logger.info(f"Generated email-compatible newsletter at {email_result}")
            else:
                logger.warning("Failed to generate email-compatible newsletter")
            
        # Update the district's top_level.json with the revised report filename
        try:
            # Get the district directory path
            script_dir = Path(__file__).parent
            dashboard_dir = script_dir / 'output' / 'dashboard'
            district_dir = dashboard_dir / district
            
            # Read the current top_level.json
            top_level_file = district_dir / 'top_level.json'
            if top_level_file.exists():
                with open(top_level_file, 'r', encoding='utf-8') as f:
                    top_level_data = json.load(f)
                
                # Add the revised report filename
                top_level_data['monthly_report'] = Path(revised_newsletter_path).name
                
                # Save the updated top_level.json
                with open(top_level_file, 'w', encoding='utf-8') as f:
                    json.dump(top_level_data, f, indent=2)
                logger.info(f"Updated top_level.json for district {district} with revised report filename")
            else:
                logger.warning(f"top_level.json not found for district {district}")
        except Exception as e:
            logger.error(f"Error updating top_level.json: {str(e)}")
            # Don't fail the process if this update fails
            
        logger.info("Monthly newsletter process completed successfully")
        return {
            "status": "success",
            "newsletter_path": newsletter_result.get("report_path"),
            "revised_newsletter_path": revised_newsletter_path,
            "email_newsletter_path": email_result
         }
        
    except Exception as e:
        error_msg = f"Error in run_monthly_report_process: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def get_monthly_reports_list():
    """
    Get a list of all monthly newsletters from the database.
    
    Returns:
        List of newsletter objects with id, report_date, district, filename, etc.
    """
    logger.info("Getting list of monthly newsletters from database")
    
    def get_reports_operation(connection):
        cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Query the reports table
        cursor.execute("""
            SELECT id, district, period_type, max_items, original_filename, revised_filename, 
                   created_at, updated_at, published_url
            FROM reports
            ORDER BY created_at DESC
        """)
        
        reports = cursor.fetchall()
        
        # Format the reports
        formatted_reports = []
        for report in reports:
            # Format district name
            district = report['district']
            district_name = f"District {district}"
            if district == "0":
                district_name = "Citywide"
            
            # Extract date from filename (format: monthly_report_0_2025_04.html)
            filename = report['original_filename']
            parts = filename.split('_')
            
            if len(parts) >= 4:
                year = parts[3]
                month = parts[4].split('.')[0]  # Remove .html extension
                report_date = f"{year}-{month}-01T00:00:00"  # ISO format date
            else:
                # Fallback to created_at if filename doesn't match expected format
                report_date = report['created_at'].isoformat()
            
            # Count the number of items for this report
            cursor.execute("""
                SELECT COUNT(*) FROM monthly_reporting WHERE report_id = %s
            """, (report['id'],))
            
            item_count = cursor.fetchone()[0]
            
            # Create a report object
            formatted_reports.append({
                "id": report['id'],
                "report_date": report_date,
                "district": district,
                "district_name": district_name,
                "period_type": report['period_type'],
                "max_items": report['max_items'],
                "item_count": item_count,
                "original_filename": report['original_filename'],
                "revised_filename": report['revised_filename'],
                "published_url": report['published_url'],
                "created_at": report['created_at'].isoformat(),
                "updated_at": report['updated_at'].isoformat()
            })
        
        cursor.close()
        return formatted_reports
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=get_reports_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] == "success":
        return result["result"]
    else:
        logger.error(f"Error getting monthly reports: {result['message']}")
        return []

def delete_monthly_report(report_id):
    """
    Delete a specific monthly newsletter file and its database records.
    
    Args:
        report_id: The ID of the newsletter to delete (index in the list)
        
    Returns:
        Status dictionary
    """
    logger.info(f"Deleting monthly newsletter with ID {report_id}")
    
    def delete_report_operation(connection):
        cursor = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Get the report details
        cursor.execute("""
            SELECT id, original_filename, revised_filename
            FROM reports
            WHERE id = %s
        """, (report_id,))
        
        report = cursor.fetchone()
        if not report:
            cursor.close()
            return None
        
        # Delete records from monthly_reporting table
        cursor.execute("""
            DELETE FROM monthly_reporting
            WHERE report_id = %s
        """, (report_id,))
        
        # Delete record from reports table
        cursor.execute("""
            DELETE FROM reports
            WHERE id = %s
        """, (report_id,))
        
        connection.commit()
        cursor.close()
        
        return dict(report)
    
    # Execute the operation with proper connection handling
    result = execute_with_connection(
        operation=delete_report_operation,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME,
        db_user=DB_USER,
        db_password=DB_PASSWORD
    )
    
    if result["status"] != "success":
        return {"status": "error", "message": result["message"]}
    
    if not result["result"]:
        return {"status": "error", "message": f"Report with ID {report_id} not found in database"}
    
    # Now delete the files
    try:
        report = result["result"]
        reports_dir = Path(__file__).parent / 'output' / 'reports'
        
        # Check if the directory exists
        if not reports_dir.exists():
            logger.warning(f"Reports directory does not exist: {reports_dir}")
            return {"status": "error", "message": "Reports directory not found"}
        
        # Delete original file
        original_path = reports_dir / report['original_filename']
        if original_path.exists():
            original_path.unlink()
            logger.info(f"Deleted original newsletter file: {original_path}")
        
        # Delete revised file if it exists
        if report['revised_filename']:
            revised_path = reports_dir / report['revised_filename']
            if revised_path.exists():
                revised_path.unlink()
                logger.info(f"Deleted revised newsletter file: {revised_path}")
        
        return {"status": "success", "message": f"Newsletter {report['original_filename']} and its database records deleted successfully"}
    except Exception as e:
        error_msg = f"Error deleting newsletter files: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "message": error_msg}

def generate_chart_image(chart_type, params, output_dir=None):
    """
    Generate a static image of a chart for email compatibility.
    
    Args:
        chart_type: Type of chart ('time_series' or 'anomaly')
        params: Parameters for the chart (metric_id, district, etc.)
        output_dir: Directory to save the image (defaults to a temporary directory)
        
    Returns:
        Path to the generated image file
    """
    logger.info(f"Generating static image for {chart_type} chart with params: {params}")
    
    try:
        # Create output directory if not provided
        if not output_dir:
            output_dir = Path(__file__).parent / 'output' / 'charts'
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate a unique filename
        timestamp = int(time.time())
        filename = f"{chart_type}_{timestamp}.png"
        output_path = output_dir / filename
        
        # Construct the URL for the chart
        if chart_type == 'time_series':
            url = f"/backend/time-series-chart?metric_id={params.get('metric_id', 1)}&district={params.get('district', 0)}&period_type={params.get('period_type', 'year')}#chart-section"
        elif chart_type == 'anomaly':
            url = f"/anomaly-analyzer/anomaly-chart?id={params.get('id', 27338)}#chart-section"
        else:
            raise ValueError(f"Unsupported chart type: {chart_type}")
        
        # Use a headless browser service to capture the chart as an image
        # This could be implemented using Selenium, Playwright, or a service like Browserless
        # For this example, we'll use a simple approach with requests and PIL
        
        # Make a request to the chart URL
        full_url = f"{API_BASE_URL}{url}"  # Only use API_BASE_URL for server-side requests
        response = requests.get(full_url)
        if response.status_code != 200:
            logger.error(f"Failed to fetch chart from {full_url}: {response.status_code}")
            return None
        
        # For a real implementation, you would use a headless browser to render the page
        # and capture the chart element as an image
        # For this example, we'll create a placeholder image
        
        # Create a placeholder image (in a real implementation, this would be the actual chart)
        img = Image.new('RGB', (800, 450), color=(255, 255, 255))
        
        # Save the image
        img.save(output_path)
        
        logger.info(f"Generated chart image at {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating chart image: {str(e)}", exc_info=True)
        return None

def generate_email_compatible_report(report_path, output_path=None):
    """
    Generate an email-compatible version of the report by replacing iframes with static images.
    
    Args:
        report_path: Path to the original report HTML file
        output_path: Path to save the email-compatible report (defaults to report_path with _email suffix)
        
    Returns:
        Path to the email-compatible report
    """
    logger.info(f"Generating email-compatible version of report: {report_path}")
    
    try:
        # Set default output path if not provided
        if not output_path:
            report_path_obj = Path(report_path)
            output_path = report_path_obj.parent / f"{report_path_obj.stem}_email{report_path_obj.suffix}"
        
        # Read the original report
        with open(report_path, 'r', encoding='utf-8') as f:
            report_html = f.read()
        
        # Expand chart references FIRST if they exist (e.g., [CHART:...])
        # This ensures we are working with iframe tags
        logger.info(f"Expanding chart references in report for email compatibility: {report_path}")
        expand_result = expand_chart_references(report_path) # This modifies the file in place
        if not expand_result:
            logger.warning("Failed to expand chart references in the report for email compatibility")
            
        # Read the potentially modified report again
        with open(report_path, 'r', encoding='utf-8') as f:
            report_html = f.read()
        
        # Create directory for charts if it doesn't exist within the reports directory
        reports_dir = Path(report_path).parent
        charts_dir = reports_dir / 'charts'
        charts_dir.mkdir(exist_ok=True)
        
        # Find all iframe elements using a more robust regex
        # This regex looks for <iframe> tags and captures the src attribute value
        iframe_pattern = r'<iframe.*?src=["\']([^"\']+)["\'].*?>.*?</iframe>'
        
        # Also search for entire chart containers with iframes inside
        chart_container_pattern = r'<div class="chart-container".*?>.*?<iframe.*?src=["\']([^"\']+)["\'].*?>.*?</iframe>.*?</div>'
        
        # Combine found iframes from both patterns
        iframe_matches = re.findall(iframe_pattern, report_html, re.IGNORECASE | re.DOTALL)
        chart_container_matches = re.findall(chart_container_pattern, report_html, re.IGNORECASE | re.DOTALL)
        
        # Combine unique iframe URLs
        iframe_urls = list(set(iframe_matches + chart_container_matches))
        
        logger.info(f"Found {len(iframe_urls)} potential iframe URLs to process.")
        
        processed_html = report_html
        replacements_made = 0
        
        # Replace each iframe with a static image
        for iframe_url_encoded in iframe_urls:
            # Decode HTML entities like &amp;
            import html
            iframe_url = html.unescape(iframe_url_encoded)

            # Use a stripped version for regex matching to avoid issues with leading/trailing whitespace
            iframe_url_to_match = iframe_url_encoded.strip()

            logger.info(f"Processing iframe URL (original captured): '{iframe_url_encoded}'")
            logger.info(f"Processing iframe URL (stripped for matching): '{iframe_url_to_match}'")
            logger.info(f"Processing iframe URL (unescaped for params): '{iframe_url}'")
            
            # Extract chart type and parameters from the URL
            if 'time-series-chart' in iframe_url:
                chart_type = 'time_series'
                # Extract parameters from URL
                params = {}
                if '?' in iframe_url:
                    # Handle potential fragments before splitting params
                    url_parts = iframe_url.split('?')[1]
                    query_string = url_parts.split('#')[0]
                    for param in query_string.split('&'):
                        if '=' in param:
                            key, value = param.split('=')
                            params[key] = value
                        else: # Handle cases where a param might not have a value
                            params[param] = ''
                
                metric_id = params.get('metric_id', 'unknown')
                district = params.get('district', 'unknown')
                period_type = params.get('period_type', 'unknown')
                
                # Generate a unique filename
                timestamp = int(time.time())
                img_filename = f"time_series_{metric_id}_{district}_{period_type}_{timestamp}.png"
                img_path = charts_dir / img_filename
                
                logger.info(f"Requesting time-series image for metric={metric_id}, district={district}, period={period_type}")
                # Request the chart image
                success = request_chart_image('time_series', params, img_path)
                
                if success:
                    # Create an img tag with a relative path using fixed chart container structure
                    img_tag = f'<div class="chart-container" style="margin-bottom: 30px; background-color: transparent; padding: 0;"><div style="position: relative; width: 100%; height: 0; padding-bottom: 100%;"><div style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"><img src="charts/{img_filename}" alt="Time Series Chart for Metric {metric_id}" style="max-width: 100%; height: 100%; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);" /></div></div></div>'
                    
                    # More specific regex for replacement to avoid unintended changes
                    # First, try to match and replace the entire chart container if it exists
                    chart_container_pattern = r'<div class="chart-container".*?>.*?<iframe.*?src=["\']' + re.escape(iframe_url_to_match) + r'["\'].*?>.*?</iframe>.*?</div>'
                    new_html, container_count = re.subn(chart_container_pattern, img_tag, processed_html, flags=re.IGNORECASE | re.DOTALL)
                    
                    if container_count > 0:
                        processed_html = new_html
                        replacements_made += container_count
                        logger.info(f"Replaced entire chart container with iframe (src: {iframe_url_to_match}) with image container: {img_filename} ({container_count} occurrence(s))")
                    else:
                        # If no chart container was found, try matching just the iframe
                        specific_iframe_pattern = r'<iframe.*?src=["\']' + re.escape(iframe_url_to_match) + r'["\'].*?>.*?</iframe>'
                        new_html, iframe_count = re.subn(specific_iframe_pattern, img_tag, processed_html, flags=re.IGNORECASE | re.DOTALL)
                        
                        if iframe_count > 0:
                            processed_html = new_html
                            replacements_made += iframe_count
                            logger.info(f"Replaced time series chart iframe (src: {iframe_url_to_match}) with image: {img_filename} ({iframe_count} occurrence(s))")
                        else:
                            logger.warning(f"Could not find exact iframe tag to replace for src: {iframe_url_to_match}")
                else:
                    logger.warning(f"Failed to generate image for time series chart ({iframe_url}), skipping replacement.")
                
            elif 'anomaly-chart' in iframe_url:
                chart_type = 'anomaly'
                # Extract parameters from URL
                params = {}
                if '?' in iframe_url:
                    url_parts = iframe_url.split('?')[1]
                    query_string = url_parts.split('#')[0]
                    for param in query_string.split('&'):
                        if '=' in param:
                            key, value = param.split('=')
                            params[key] = value
                        else:
                             params[param] = ''
                
                anomaly_id = params.get('id', 'unknown')
                
                # Generate a unique filename
                timestamp = int(time.time())
                img_filename = f"anomaly_{anomaly_id}_{timestamp}.png"
                img_path = charts_dir / img_filename
                
                logger.info(f"Requesting anomaly image for id={anomaly_id}")
                # Request the chart image
                success = request_chart_image('anomaly', params, img_path)
                
                if success:
                    # Create an img tag with a relative path using fixed chart container structure
                    img_tag = f'<div class="chart-container" style="margin-bottom: 30px; background-color: transparent; padding: 0;"><div style="position: relative; width: 100%; height: 0; padding-bottom: 100%;"><div style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"><img src="charts/{img_filename}" alt="Anomaly Chart for ID {anomaly_id}" style="max-width: 100%; height: 100%; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);" /></div></div></div>'
                    
                    # More specific regex for replacement
                    specific_iframe_pattern = r'<iframe.*?src=["\']' + re.escape(iframe_url_to_match) + r'["\'].*?>.*?</iframe>'
                    new_html, count = re.subn(specific_iframe_pattern, img_tag, processed_html, flags=re.IGNORECASE | re.DOTALL)
                    if count > 0:
                        processed_html = new_html
                        replacements_made += count
                        logger.info(f"Replaced anomaly chart iframe (src: {iframe_url_to_match}) with image: {img_filename} ({count} occurrence(s))")
                    else:
                         logger.warning(f"Could not find exact iframe tag to replace for src: {iframe_url_to_match}")
                else:
                     logger.warning(f"Failed to generate image for anomaly chart ({iframe_url}), skipping replacement.")
            else:
                logger.warning(f"Skipping unsupported iframe URL: {iframe_url}")
                
        logger.info(f"Total iframe replacements made: {replacements_made}")
        
        # # Add a note at the top of the email about interactive charts (only if replacements were made)
        # if replacements_made > 0:
        #     note_html = '<div style="padding: 15px; margin-top: 20px; margin-bottom: 20px; background-color: #f8f9fa; border-left: 4px solid #007BFF; border-radius: 4px;"><p style="margin: 0; font-size: 14px;">Some interactive charts have been replaced with static images in this email version. <a href="https://transparentsf.com" style="color: #007BFF; text-decoration: underline;">View the full interactive version online</a>.</p></div>'
            
        #     # Find a good place to insert the note - after the logo container if it exists
        #     if '<div class="logo-container">' in processed_html:
        #         # Try to insert after the closing div of the logo container
        #         insert_point = processed_html.find('<div class="logo-container">')
        #         if insert_point != -1:
        #             end_logo_div = processed_html.find('</div>', insert_point)
        #             if end_logo_div != -1:
        #                  processed_html = processed_html[:end_logo_div+6] + '\n    ' + note_html + processed_html[end_logo_div+6:]
        #             else: # Fallback if closing div not found immediately
        #                  processed_html = processed_html.replace('<body>', '<body>\n    ' + note_html)
        #         else:
        #             processed_html = processed_html.replace('<body>', '<body>\n    ' + note_html)
        #     else:
        #         # Insert at the beginning of the body as fallback
        #         processed_html = processed_html.replace('<body>', '<body>\n    ' + note_html)

        #     # Add a footer note with link to full version
        #     footer_note = '<div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; text-align: center; font-size: 13px; color: #666;">This is an email version with some interactive elements replaced by static images. <a href="https://transparentsf.com" style="color: #007BFF;">View the full interactive version online</a>.</div>'
            
            # # Add the footer note before the closing body tag
            # if '</body>' in processed_html:
            #     processed_html = processed_html.replace('</body>', footer_note + '\n</body>')
            # else:
            #     processed_html += footer_note # Append if no body tag found
        
        # Write the processed HTML to the output file
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(processed_html)
        
        logger.info(f"Generated email-compatible report at {output_path}")
        return str(output_path)
        
    except Exception as e:
        logger.error(f"Error generating email-compatible report: {str(e)}", exc_info=True)
        return None

def generate_chart_data_url(chart_type, params):
    """
    Generate a chart as a data URL for direct embedding in HTML.
    
    Args:
        chart_type: Type of chart ('time_series' or 'anomaly')
        params: Parameters for the chart (metric_id, district, etc.)
        
    Returns:
        Data URL of the chart image
    """
    logger.info(f"Generating data URL for {chart_type} chart with params: {params}")
    
    try:
        # Construct the URL for the chart
        if chart_type == 'time_series':
            url = f"/backend/time-series-chart?metric_id={params.get('metric_id', 1)}&district={params.get('district', 0)}&period_type={params.get('period_type', 'year')}#chart-section"
        elif chart_type == 'anomaly':
            url = f"/anomaly-analyzer/anomaly-chart?id={params.get('id', 27338)}#chart-section"
        else:
            raise ValueError(f"Unsupported chart type: {chart_type}")
        
        # Use a headless browser service to capture the chart as an image
        # This could be implemented using Selenium, Playwright, or a service like Browserless
        # For this example, we'll use a simple approach with requests and PIL
        
        # Make a request to the chart URL
        full_url = f"{API_BASE_URL}{url}"  # Only use API_BASE_URL for server-side requests
        response = requests.get(full_url)
        if response.status_code != 200:
            logger.error(f"Failed to fetch chart from {full_url}: {response.status_code}")
            return None
        
        # For a real implementation, you would use a headless browser to render the page
        # and capture the chart element as an image
        # For this example, we'll create a placeholder image
        
        # Create a placeholder image (in a real implementation, this would be the actual chart)
        img = Image.new('RGB', (800, 450), color=(255, 255, 255))
        
        # Convert the image to a data URL
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        data_url = f"data:image/png;base64,{img_str}"
        
        logger.info(f"Generated data URL for {chart_type} chart")
        return data_url
        
    except Exception as e:
        logger.error(f"Error generating chart data URL: {str(e)}", exc_info=True)
        return None

def generate_inline_report(report_path, output_path=None):
    """
    Generate a report with inline data URLs for charts, making it suitable for email.
    
    Args:
        report_path: Path to the original report HTML file
        output_path: Path to save the inline report (defaults to report_path with _inline suffix)
        
    Returns:
        Path to the inline report
    """
    logger.info(f"Generating inline version of report: {report_path}")
    
    try:
        # Set default output path if not provided
        if not output_path:
            report_path_obj = Path(report_path)
            output_path = report_path_obj.parent / f"{report_path_obj.stem}_inline{report_path_obj.suffix}"
        
        # Read the original report
        with open(report_path, 'r', encoding='utf-8') as f:
            report_html = f.read()
            
        # Expand chart references in the report
        logger.info(f"Expanding chart references in report for inline version: {report_path}")
        expand_result = expand_chart_references(report_path)
        if not expand_result:
            logger.warning("Failed to expand chart references in the report for inline version")
            
        # Read the report again after expanding chart references
        with open(report_path, 'r', encoding='utf-8') as f:
            report_html = f.read()
        
        # Find all iframe elements
        import re
        iframe_pattern = r'<iframe[^>]*src="([^"]*)"[^>]*></iframe>'
        iframes = re.findall(iframe_pattern, report_html)
        
        # Replace each iframe with an inline image
        for iframe_url in iframes:
            # Extract chart type and parameters from the URL
            if 'time-series-chart' in iframe_url:
                # Extract parameters from URL
                params = {}
                for param in iframe_url.split('?')[1].split('&'):
                    key, value = param.split('=')
                    params[key] = value
                
                # Generate a data URL for the time series chart
                data_url = generate_time_series_chart_data_url(
                    metric_id=params.get('metric_id', 1),
                    district=params.get('district', 0),
                    period_type=params.get('period_type', 'year')
                )
                if not data_url:
                    logger.warning(f"Failed to generate data URL for {iframe_url}")
                    continue
                
                # Create an img tag with the data URL
                img_tag = f'<img src="{data_url}" alt="Time Series Chart" style="max-width: 100%; height: auto;" />'
                
                # Replace the iframe with the img tag
                iframe_tag = f'<iframe[^>]*src="{iframe_url}"[^>]*></iframe>'
                report_html = re.sub(iframe_tag, img_tag, report_html)
                
            elif 'anomaly-chart' in iframe_url:
                chart_type = 'anomaly'
                # Extract parameters from URL
                params = {}
                for param in iframe_url.split('?')[1].split('&'):
                    key, value = param.split('=')
                    params[key] = value
                
                # Generate a data URL for the chart
                data_url = generate_chart_data_url(chart_type, params)
                if not data_url:
                    logger.warning(f"Failed to generate data URL for {iframe_url}")
                    continue
                
                # Create an img tag with the data URL
                img_tag = f'<img src="{data_url}" alt="Chart" style="max-width: 100%; height: auto;" />'
                
                # Replace the iframe with the img tag
                iframe_tag = f'<iframe[^>]*src="{iframe_url}"[^>]*></iframe>'
                report_html = re.sub(iframe_tag, img_tag, report_html)
            else:
                logger.warning(f"Unsupported iframe URL: {iframe_url}")
                continue
        
        # Write the inline report
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report_html)
        
        logger.info(f"Generated inline report at {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating inline report: {str(e)}", exc_info=True)
        return None

def generate_time_series_chart_image(metric_id, district, period_type, output_dir=None):
    """
    Generate a static image of a time series chart for email compatibility.
    
    Args:
        metric_id: ID of the metric to chart
        district: District number
        period_type: Period type (year, month, etc.)
        output_dir: Directory to save the image (defaults to a temporary directory)
        
    Returns:
        Path to the generated image file
    """
    logger.info(f"Generating static image for time series chart: metric_id={metric_id}, district={district}, period_type={period_type}")
    
    try:
        # Create output directory if not provided
        if not output_dir:
            output_dir = Path(__file__).parent / 'output' / 'charts'
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate a unique filename
        timestamp = int(time.time())
        filename = f"time_series_{metric_id}_{district}_{period_type}_{timestamp}.png"
        output_path = output_dir / filename
        
        # Construct the URL for the chart
        url = f"/backend/time-series-chart?metric_id={metric_id}&district={district}&period_type={period_type}#chart-section"
        
        # Use a headless browser service to capture the chart as an image
        # This could be implemented using Selenium, Playwright, or a service like Browserless
        # For this example, we'll use a simple approach with requests and PIL
        
        # Make a request to the chart URL
        full_url = f"{API_BASE_URL}{url}"  # Only use API_BASE_URL for server-side requests
        response = requests.get(full_url)
        if response.status_code != 200:
            logger.error(f"Failed to fetch chart from {full_url}: {response.status_code}")
            return None
        
        # For a real implementation, you would use a headless browser to render the page
        # and capture the chart element as an image
        # For this example, we'll create a placeholder image
        
        # Create a placeholder image (in a real implementation, this would be the actual chart)
        img = Image.new('RGB', (800, 450), color=(255, 255, 255))
        
        # Save the image
        img.save(output_path)
        
        logger.info(f"Generated time series chart image at {output_path}")
        return output_path
        
    except Exception as e:
        logger.error(f"Error generating time series chart image: {str(e)}", exc_info=True)
        return None

def generate_time_series_chart_data_url(metric_id, district, period_type):
    """
    Generate a time series chart as a data URL for direct embedding in HTML.
    
    Args:
        metric_id: ID of the metric to chart
        district: District number
        period_type: Period type (year, month, etc.)
        
    Returns:
        Data URL of the chart image
    """
    logger.info(f"Generating data URL for time series chart: metric_id={metric_id}, district={district}, period_type={period_type}")
    
    try:
        # Construct the URL for the chart
        url = f"/backend/time-series-chart?metric_id={metric_id}&district={district}&period_type={period_type}#chart-section"
        
        # Use a headless browser service to capture the chart as an image
        # This could be implemented using Selenium, Playwright, or a service like Browserless
        # For this example, we'll use a simple approach with requests and PIL
        
        # Make a request to the chart URL
        full_url = f"{API_BASE_URL}{url}"  # Only use API_BASE_URL for server-side requests
        response = requests.get(full_url)
        if response.status_code != 200:
            logger.error(f"Failed to fetch chart from {full_url}: {response.status_code}")
            return None
        
        # For a real implementation, you would use a headless browser to render the page
        # and capture the chart element as an image
        # For this example, we'll create a placeholder image
        
        # Create a placeholder image (in a real implementation, this would be the actual chart)
        img = Image.new('RGB', (800, 450), color=(255, 255, 255))
        
        # Convert the image to a data URL
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        data_url = f"data:image/png;base64,{img_str}"
        
        logger.info(f"Generated data URL for time series chart")
        return data_url
        
    except Exception as e:
        logger.error(f"Error generating time series chart data URL: {str(e)}", exc_info=True)
        return None

class PathEncoder(json.JSONEncoder):
    """Custom JSON encoder that can handle Path objects"""
    def default(self, obj):
        if isinstance(obj, Path):
            return str(obj)
        return super().default(obj)

def expand_chart_references(report_path):
    """
    Process a report file and replace simplified chart references with full HTML.
    
    Args:
        report_path: Path to the report file to process
        
    Returns:
        Path to the processed report file
    """
    logger.info(f"Expanding chart references in report: {report_path}")
    
    try:
        # Read the report file
        with open(report_path, 'r', encoding='utf-8') as f:
            report_html = f.read()
        
        # Define patterns for simplified chart references
        time_series_pattern = r'\[CHART:time_series:(\d+):(\d+):(\w+)\]'
        anomaly_pattern = r'\[CHART:anomaly:([a-zA-Z0-9]+)\]'  # Changed to support alphanumeric IDs
        
        # Define pattern for direct image references
        image_pattern_with_alt = r'<img[^>]*src="([^"]+)"[^>]*alt="([^"]+)"[^>]*>'
        image_pattern_without_alt = r'<img[^>]*src="([^"]+)"[^>]*>'
        
        # Replace time series chart references
        def replace_time_series(match):
            metric_id = match.group(1)
            district = match.group(2)
            period_type = match.group(3)
            
            return f"""
<div class="chart-container">
    <div style="position: relative; width: 100%; padding-bottom: 100%;">
        <iframe src="/backend/time-series-chart?metric_id={metric_id}&district={district}&period_type={period_type}#chart-section" 
                style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none;" 
                frameborder="0" 
                scrolling="no">
        </iframe>
    </div>
</div>"""
        
        # Replace anomaly chart references
        def replace_anomaly(match):
            anomaly_id = match.group(1)
            
            return f"""
<div class="chart-container">
    <div style="position: relative; width: 100%; padding-bottom: 100%;">
        <iframe src="/anomaly-analyzer/anomaly-chart?id={anomaly_id}#chart-section" 
                style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none;" 
                frameborder="0" 
                scrolling="no">
        </iframe>
    </div>
</div>"""
        
        # Replace direct image references with iframes
        def replace_image_with_alt(match):
            img_tag = match.group(0)
            img_src = match.group(1)
            img_alt = match.group(2)
            
            # Check if it's an anomaly chart image
            if img_src.startswith("anomaly_"):
                # Extract anomaly ID from the filename
                parts = img_src.split("_")
                if len(parts) >= 2:
                    anomaly_id = parts[1].split(".")[0]  # Remove file extension
                    
                    return f"""
<div class="chart-container">
    <div style="position: relative; width: 100%; padding-bottom: 100%;">
        <iframe src="/anomaly-analyzer/anomaly-chart?id={anomaly_id}#chart-section" 
                style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none;" 
                frameborder="0" 
                scrolling="no">
        </iframe>
    </div>
</div>"""
            
            # If we can't determine the chart type, keep the original image
            return img_tag
        
        def replace_image_without_alt(match):
            img_tag = match.group(0)
            img_src = match.group(1)
            
            # Check if it's an anomaly chart image
            if img_src.startswith("anomaly_"):
                # Extract anomaly ID from the filename
                parts = img_src.split("_")
                if len(parts) >= 2:
                    anomaly_id = parts[1].split(".")[0]  # Remove file extension
                    
                    return f"""
<div class="chart-container">
    <div style="position: relative; width: 100%; padding-bottom: 100%;">
        <iframe src="/anomaly-analyzer/anomaly-chart?id={anomaly_id}#chart-section" 
                style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border: none;" 
                frameborder="0" 
                scrolling="no">
        </iframe>
    </div>
</div>"""
            
            # If we can't determine the chart type, keep the original image
            return img_tag
        
        # Apply replacements
        report_html = re.sub(time_series_pattern, replace_time_series, report_html)
        report_html = re.sub(anomaly_pattern, replace_anomaly, report_html)
        report_html = re.sub(image_pattern_with_alt, replace_image_with_alt, report_html)
        report_html = re.sub(image_pattern_without_alt, replace_image_without_alt, report_html)
        
        # Write the processed report
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report_html)
        
        logger.info(f"Expanded chart references in report: {report_path}")
        return report_path
        
    except Exception as e:
        error_msg = f"Error in expand_chart_references: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return report_path

def request_chart_image(chart_type, params, output_path):
    """
    Request a rendered chart image from the chart endpoint using Playwright to capture the exact
    rendered chart with the original styling from the chart.html files.
    
    Args:
        chart_type: Type of chart ('time_series' or 'anomaly')
        params: Parameters for the chart (metric_id, district, etc.)
        output_path: Path to save the image
        
    Returns:
        Boolean indicating success
    """
    logger.info(f"Requesting {chart_type} chart image with params: {params}")
    
    try:
        from playwright.sync_api import sync_playwright
        
        # Construct the URL for the chart based on chart type
        if chart_type == 'time_series':
            metric_id = params.get('metric_id', '1')
            district = params.get('district', '0')
            period_type = params.get('period_type', 'year')
            
            # Clean period_type: Remove any fragments like #chart-section
            if '#' in period_type:
                period_type = period_type.split('#')[0]
                logger.debug(f"Cleaned period_type to: {period_type}")
            
            url = f"{API_BASE_URL}/backend/time-series-chart?metric_id={metric_id}&district={district}&period_type={period_type}#chart-section"
            landing_page_url = f"{API_BASE_URL}/backend/metric/{metric_id}?district={district}"
        elif chart_type == 'anomaly':
            anomaly_id = params.get('id', '27338')
            url = f"{API_BASE_URL}/anomaly-analyzer/anomaly-chart?id={anomaly_id}#chart-section"
            landing_page_url = f"{API_BASE_URL}/anomaly-analyzer/anomaly/{anomaly_id}"
        else:
            logger.error(f"Unsupported chart type: {chart_type}")
            return False
        
        logger.info(f"Capturing chart from URL: {url}")
        
        with sync_playwright() as p:
            # Launch browser with larger viewport to ensure chart is fully visible
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={'width': 1200, 'height': 800},
                device_scale_factor=2.0  # Higher resolution for sharper images
            )
            page = context.new_page()
            
            # Navigate to the chart URL and wait for load
            page.goto(url, wait_until="networkidle")
            
            # Wait for the chart to be fully rendered
            try:
                if chart_type == 'time_series':
                    # Wait for the chart container to be visible
                    page.wait_for_selector('#chart-section', state='visible', timeout=10000)
                    
                    # Then wait for plotly to be fully loaded
                    page.wait_for_function('''
                        () => {
                            const plotDiv = document.querySelector('.js-plotly-plot');
                            return plotDiv && 
                                  plotDiv.querySelector('.main-svg') && 
                                  !document.querySelector('.plot-container .loader');
                        }
                    ''', timeout=15000)
                    
                    # Get the chart container element
                    element_selector = '#chart-section'
                else:  # anomaly
                    # Wait for the container to be visible
                    page.wait_for_selector('#anomaly-chart', state='visible', timeout=10000)
                    
                    # Then wait for plotly to be fully loaded
                    page.wait_for_function('''
                        () => {
                            const plotDiv = document.querySelector('.js-plotly-plot');
                            return plotDiv && 
                                  plotDiv.querySelector('.main-svg') && 
                                  !document.querySelector('.plot-container .loader');
                        }
                    ''', timeout=15000)
                    
                    # Get the chart container element
                    element_selector = '#anomaly-chart'
            except Exception as wait_error:
                # If specific selectors fail, try a more general approach
                logger.warning(f"Specific selectors failed, trying general approach: {wait_error}")
                # Just wait for any plotly chart to appear
                page.wait_for_selector('.js-plotly-plot', state='visible', timeout=15000)
                
                # Use a more general selector
                if chart_type == 'time_series':
                    element_selector = '.chart-container'
                else:  # anomaly
                    element_selector = '.chart-container'
            
            # Give a little extra time for animations to complete
            page.wait_for_timeout(1000)
            
            # Make sure the chart container is properly styled with the corner decorations
            # AND remove the Full View and Share buttons
            page.evaluate(f'''() => {{
                // Make sure the chart container has the proper styling
                const chartContainer = document.querySelector('.chart-container');
                if (chartContainer) {{
                    // Ensure the container is visible and properly sized
                    chartContainer.style.display = 'block';
                    chartContainer.style.position = 'relative';
                    chartContainer.style.width = '600px';
                    chartContainer.style.height = '600px';
                    chartContainer.style.maxWidth = '600px';
                    chartContainer.style.backgroundColor = 'white';
                    chartContainer.style.borderRadius = '8px';
                    chartContainer.style.padding = '10px';
                    chartContainer.style.margin = '0 auto';
                    chartContainer.style.boxShadow = 'none';
                    chartContainer.style.overflow = 'visible';
                    
                    // Make sure the ::before and ::after pseudo-elements are visible
                    // We can't directly manipulate pseudo-elements, but we can add actual elements to mimic them
                    
                    // Remove any existing corner elements
                    const existingCorners = document.querySelectorAll('.chart-corner');
                    existingCorners.forEach(el => el.remove());
                    
                    // Create top-right corner
                    const topRightCorner = document.createElement('div');
                    topRightCorner.className = 'chart-corner top-right';
                    topRightCorner.style.position = 'absolute';
                    topRightCorner.style.top = '0';
                    topRightCorner.style.right = '0';
                    topRightCorner.style.width = '61.8%';
                    topRightCorner.style.height = '61.8%';
                    topRightCorner.style.borderTop = '2px solid rgba(0, 0, 0, 0.2)';
                    topRightCorner.style.borderRight = '2px solid rgba(0, 0, 0, 0.2)';
                    topRightCorner.style.borderTopRightRadius = '8px';
                    topRightCorner.style.pointerEvents = 'none';
                    chartContainer.appendChild(topRightCorner);
                    
                    // Create bottom-left corner
                    const bottomLeftCorner = document.createElement('div');
                    bottomLeftCorner.className = 'chart-corner bottom-left';
                    bottomLeftCorner.style.position = 'absolute';
                    bottomLeftCorner.style.bottom = '0';
                    bottomLeftCorner.style.left = '0';
                    bottomLeftCorner.style.width = '61.8%';
                    bottomLeftCorner.style.height = '61.8%';
                    bottomLeftCorner.style.borderBottom = '2px solid rgba(0, 0, 0, 0.2)';
                    bottomLeftCorner.style.borderLeft = '2px solid rgba(0, 0, 0, 0.2)';
                    bottomLeftCorner.style.borderBottomLeftRadius = '8px';
                    bottomLeftCorner.style.pointerEvents = 'none';
                    chartContainer.appendChild(bottomLeftCorner);
                    
                    // Remove "Full View" and "Share" buttons
                    const buttons = document.querySelectorAll('#full-view-btn, #share-btn, .btn-share, .btn-full-view');
                    buttons.forEach(btn => {{
                        btn.remove();
                    }});
                    
                    // Add a "View Interactive Chart" link with an arrow icon
                    const linkContainer = document.createElement('div');
                    linkContainer.style.position = 'absolute';
                    linkContainer.style.bottom = '8px';
                    linkContainer.style.right = '8px';
                    linkContainer.style.zIndex = '10';
                    
                    const interactiveLink = document.createElement('a');
                    interactiveLink.href = "{landing_page_url}";
                    interactiveLink.target = "_blank";
                    interactiveLink.style.padding = '8px 12px';
                    interactiveLink.style.backgroundColor = 'rgba(0, 0, 0, 0.7)';
                    interactiveLink.style.color = 'white';
                    interactiveLink.style.borderRadius = '4px';
                    interactiveLink.style.textDecoration = 'none';
                    interactiveLink.style.fontSize = '12px';
                    interactiveLink.style.fontWeight = 'bold';
                    interactiveLink.style.display = 'flex';
                    interactiveLink.style.alignItems = 'center';
                    interactiveLink.style.gap = '4px';
                    interactiveLink.innerHTML = 'View Interactive Chart <span>→</span>';
                    
                    linkContainer.appendChild(interactiveLink);
                    chartContainer.appendChild(linkContainer);
                    
                    // Make sure the chart is visible
                    const chartElement = document.querySelector('#time-series-chart, #anomaly-chart');
                    if (chartElement) {{
                        chartElement.style.display = 'block';
                        chartElement.style.width = '100%';
                        chartElement.style.height = '100%';
                    }}
                    
                    // Make sure the plot is visible
                    const plotElement = document.querySelector('#time-series-plot, #anomaly-plot');
                    if (plotElement) {{
                        plotElement.style.display = 'block';
                        plotElement.style.width = '100%';
                        plotElement.style.height = '100%';
                    }}
                }}
            }}''')
            
            # Wait a moment for the styling to be applied
            page.wait_for_timeout(500)
            
            # Take the screenshot of the chart container
            chart_container = page.locator('.chart-container')
            if chart_container.count() > 0:
                chart_container.screenshot(path=output_path)
                logger.info(f"Successfully captured chart image to {output_path}")
            else:
                # Fallback to the specific element selector if chart container not found
                element = page.locator(element_selector)
                element.screenshot(path=output_path)
                logger.info(f"Captured chart image using fallback selector: {element_selector}")
            
            # Close browser
            browser.close()
            
            return True
            
    except Exception as e:
        logger.error(f"Error capturing chart image: {e}", exc_info=True)
        # If Playwright fails, try to create a fallback image
        logger.warning(f"Falling back to basic chart image for {chart_type}.")
        create_fallback_chart_image(chart_type, params, output_path)
        return True

def create_fallback_chart_image(chart_type, params, output_path):
    """
    Create a fallback chart image with placeholder text.
    
    Args:
        chart_type: Type of chart ('time_series' or 'anomaly')
        params: Parameters for the chart (metric_id, district, etc.)
        output_path: Path to save the image
    """
    try:
        # Create a simple plot with Plotly
        fig = go.Figure()
        
        # Add text explaining this is a placeholder
        if chart_type == 'time_series':
            metric_id = params.get('metric_id', '1')
            district = params.get('district', '0')
            district_name = "Citywide" if district == '0' else f"District {district}"
            title = f"Time Series Chart - Metric {metric_id} - {district_name}"
        else:  # anomaly
            anomaly_id = params.get('id', '27338')
            title = f"Anomaly Chart - ID {anomaly_id}"
        
        fig.add_annotation(
            text="This chart is only available in the web version<br>Please view online for interactive charts",
            xref="paper", yref="paper",
            x=0.5, y=0.5,
            showarrow=False,
            font=dict(size=20)
        )
        
        # Set layout
        fig.update_layout(
            title=title,
            width=800,
            height=500,
            plot_bgcolor='#f8f9fa',
            paper_bgcolor='#f8f9fa',
            margin=dict(t=80, b=80, l=80, r=80)
        )
        
        # Save the figure
        fig.write_image(output_path)
        logger.info(f"Created fallback chart image at {output_path}")
    except Exception as e:
        logger.error(f"Error creating fallback chart image: {e}", exc_info=True)
        # Last resort - create a blank image
        try:
            img = Image.new('RGB', (800, 500), color=(248, 249, 250))
            img.save(output_path)
            logger.info(f"Created blank fallback image at {output_path}")
        except Exception as e2:
            logger.error(f"Error creating blank image: {e2}")

if __name__ == "__main__":
    # Run the monthly report process
    # You can adjust the max_report_items to control how many items appear in the report
    result = run_monthly_report_process(max_report_items=3, district="0")
    print(json.dumps(result, indent=2, cls=PathEncoder)) 