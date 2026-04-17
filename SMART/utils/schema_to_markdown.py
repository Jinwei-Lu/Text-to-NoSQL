import json
import os
import tiktoken
from typing import Dict, Any

def dfs_dict_md(d: Dict[str, Any], prefix: str = "", level: int = 0) -> str:
    """
    Recursively traverse a nested dictionary and format it as markdown.
    
    Args:
        d: The nested dictionary to traverse
        prefix: Current path prefix for nested fields
        level: Current nesting level for indentation
    
    Returns:
        Markdown formatted string representation of the schema
    """
    md_lines = []
    indent = "  " * level
    
    for key, value in d.items():
        current_path = prefix
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # Handle nested array of objects
            md_lines.append(f"{indent}- {key} (Array):")
            # Process the first item in the array as it represents the structure
            md_lines.append(dfs_dict_md(value[0], current_path + f"{key}.", level + 1))
        elif isinstance(value, dict):
            # Handle nested object
            md_lines.append(f"{indent}- {key} (Object):")
            md_lines.append(dfs_dict_md(value, current_path + f"{key}.", level + 1))
        else:
            # Handle field with type
            md_lines.append(f"{indent}- {key}: {value}")
    
    return "\n".join(md_lines)

def schemas_transform(db_id: str) -> str:
    """
    Convert MongoDB schema JSON to markdown format.
    
    Args:
        db_id: Database identifier used in the schema filename
    
    Returns:
        Markdown formatted string of the entire schema
    """
    folder_path = "d:/Machine_Learning/Deep_Learning/Project/MongoDB/mongodb_schema"
    file_path = os.path.join(folder_path, f"{db_id}.json")
    
    try:
        with open(file_path, "r") as f:
            schema_json = json.load(f)
    except FileNotFoundError:
        return f"Error: Schema file for database '{db_id}' not found at {file_path}"
    except json.JSONDecodeError:
        return f"Error: Invalid JSON in schema file for database '{db_id}'"
    
    md_output = []
    
    # Process each collection in the schema
    for collection, fields in schema_json.items():
        md_output.append(f"### Collection: {collection}")
        md_output.append(dfs_dict_md(fields))
        md_output.append("")  # Add blank line between collections
    
    return "\n".join(md_output).strip()

def save_markdown(db_id: str, output_dir: str = "./markdown_schemas") -> str:
    """
    Convert schema to markdown and save to file.
    
    Args:
        db_id: Database identifier
        output_dir: Directory to save markdown files
    
    Returns:
        Path to the created markdown file or error message
    """
    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate markdown content
    md_content = schemas_transform(db_id)
    
    # Save to file
    output_path = os.path.join(output_dir, f"{db_id}_schema.md")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        return f"Markdown schema saved to: {output_path}"
    except Exception as e:
        return f"Error saving markdown file: {str(e)}"

if __name__ == "__main__":
    # Example usage
    db_id = "cre_Doc_Tracking_DB"
    
    # Print markdown to console
    print(schemas_transform(db_id))
    
    # Count tokens
    encoding = tiktoken.encoding_for_model("gpt-4")
    token_count = len(encoding.encode(schemas_transform(db_id)))
    print(f"Token count: {token_count}")
