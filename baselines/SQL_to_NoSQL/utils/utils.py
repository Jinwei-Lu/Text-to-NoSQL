import json
import os

import sqlite3
import openai
from pymongo import MongoClient
from pymongo.errors import OperationFailure
import tiktoken

# 连接到MongoDB
mongo_client = MongoClient('mongodb://localhost:27017/')


def generate_reply(messages: list, model: str = "deepseek-chat", temperature: float = 0.0, n: int = 1, **kwargs):
    model_configs = {
        "gpt": ("https://openkey.cloud/v1", "sk-h6d8YnNqclxa2iap329c666a0780436699Ca9c53BeB4Ed58"),
        "deepseek": ("https://api.deepseek.com", "sk-18a4fc17122046a88e8031f88248f56b")
    }

    for key, (base_url, api_key) in model_configs.items():
        if key in model:
            break
    else:
        raise ValueError(f"Unknown model: {model}")

    client = openai.Client(base_url=base_url, api_key=api_key)

    while True:
        try:
            raw_reply = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                n=n,
                stream=False,
                **kwargs
            )
            return [choice.message.content for choice in raw_reply.choices]
        except Exception as e:
            print(f"Error occurred: {e}")

def get_SQL_Schemas(db_id:str):
    '''将SQLite数据库中的Schemas转换成markdown格式。'''
    schemas_md = ""
    db_path = f"./spider/spider/database/{db_id}/{db_id}.sqlite"
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 获取所有表名
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    
    # 遍历每个表
    for table in tables:
        table_name = table[0]
        schemas_md += f"\n### {table_name}\n"
        
        # 获取表的列信息
        cursor.execute(f"PRAGMA table_info({table_name});")
        columns = cursor.fetchall()
        
        # 添加列信息，包含数据类型和主键信息
        schemas_md += "| Column | Type | Primary Key |\n"
        schemas_md += "|--------|------|-------------|\n"
        for col in columns:
            name = col[1]
            type_ = col[2]
            is_pk = "✓" if col[5] == 1 else ""  # col[5]表示是否为主键
            schemas_md += f"| {name} | {type_} | {is_pk} |\n"
        
        # 添加外键信息（如果有）
        cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table_name}';")
        create_table_sql = cursor.fetchone()[0]
        if "FOREIGN KEY" in create_table_sql.upper():
            schemas_md += "\n**Foreign Keys:**\n"
            # 解析CREATE TABLE语句中的FOREIGN KEY约束
            for line in create_table_sql.split('\n'):
                if "FOREIGN KEY" in line.upper():
                    schemas_md += f"- {line.strip().strip(',')}\n"
    
    conn.close()
    return schemas_md.strip()

def schemas_transform(db_id:str):
    folder_path = "./mongodb_schema/"
    file_path = os.path.join(folder_path, db_id + ".json")

    with open(file_path, "r") as f:
        schemas_json = json.load(f)

    schemas_str = ""
    for collection, fields_type in schemas_json.items():
        schemas_str += f"### Table: {collection}\n"
        schemas_str += "#### Columns: " + dfs_dict(d=fields_type, prefix="") + "\n"

    return schemas_str.strip("\n").strip(",").strip()


def dfs_dict(d, prefix=""):
    """
    使用深度优先搜索遍历嵌套字典。
    
    :param d: 嵌套字典
    :param prefix: 当前节点的键路径
    """
    fields = ""
    for key, value in d.items():
        current_path = prefix
        if isinstance(value, list):
            fields += current_path + key + ", "
            current_path = prefix + f"{key}_"
            # 如果值是字典，则递归进入该子字典
            for sub_d in value:
                fields += dfs_dict(sub_d, current_path)
        else:
            fields += current_path + key + ", "

    return fields[:-1]


if __name__ == "__main__":
    db_id = "college_2"
    schemas = get_SQL_Schemas(db_id=db_id)
    print(schemas)

    # count tokens
    encoding = tiktoken.encoding_for_model("gpt-4")
    token_count = len(encoding.encode(schemas))
    print(f"Token count: {token_count}")