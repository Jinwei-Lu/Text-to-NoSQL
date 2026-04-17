from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, ForeignKey, DECIMAL, Date, VARCHAR
from sqlalchemy.sql.sqltypes import Numeric
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.engine.reflection import Inspector
from pymongo import MongoClient
import copy
from bson.decimal128 import Decimal128
import decimal


# 配置数据库连接信息
db_name = "hr_1"
db_path = f'spider/spider/database/{db_name}/{db_name}.sqlite'
sqlite_url = "sqlite:///{}".format(db_path)
mongo_url = "mongodb://localhost:27017/"

# 创建SQLAlchemy引擎和元数据对象
engine = create_engine(sqlite_url)
metadata = MetaData(bind=engine)

# 创建MongoDB客户端和数据库
mongo_client = MongoClient(mongo_url)
mongo_db = mongo_client['your_database']

# 创建Session类
Session = sessionmaker(bind=engine)

# 动态构建ORM类和关系
Base = declarative_base()
tables = {}
foreign_keys = {}
processed_docs = {}  # 用于跟踪已处理的文档以避免重复处理

def map_column_type(column_type):
    """
    映射SQLAlchemy列类型到合适的类型。
    """
    if isinstance(column_type, Integer):
        return Integer
    elif isinstance(column_type, (String, VARCHAR)):
        return String(column_type.length)
    elif isinstance(column_type, (Numeric, DECIMAL)):
        return DECIMAL(precision=column_type.precision, scale=column_type.scale)
    elif isinstance(column_type, Date):
        return Date
    else:
        raise ValueError(f"Unsupported column type: {type(column_type)}")

# def model_to_dict(row, columns):
#     """
#     将行结果转换为字典。
#     """
#     return {col['name']: getattr(row, col['name']) for col in columns}


def model_to_dict(row, columns):
    """
    将行结果转换为字典。
    """
    row_dict = {}
    for col in columns:
        value = getattr(row, col['name'])
        if isinstance(value, decimal.Decimal):
            # 将 decimal.Decimal 转换为 bson.decimal128.Decimal128
            row_dict[col['name']] = Decimal128(str(value))
        else:
            row_dict[col['name']] = value
    return row_dict



def process_foreign_keys(doc, table_name, processed_docs):
    """
    处理文档中的外键，递归地获取相关联的文档。
    """
    if doc['_id'] in processed_docs:
        return doc
    processed_docs[doc['_id']] = True
    for fk_table, fk_column in foreign_keys.get(table_name, []):
        # 获取外键表的列信息
        fk_columns = inspector.get_columns(fk_table)
        related_docs = [
            process_foreign_keys(model_to_dict(related_row, fk_columns), fk_table, copy.deepcopy(processed_docs))
            for related_row in session.query(tables[fk_table]).filter_by(**{fk_column: doc[fk_column]})
        ]
        doc[fk_table] = related_docs
    return doc


def get_primary_key(table_name):
    """
    获取表的主键。
    """
    pk_constraint = inspector.get_pk_constraint(table_name)
    return pk_constraint.get('constrained_columns') if pk_constraint else []

# 初始化inspector
inspector = Inspector.from_engine(engine)

# 反射数据库模式并构建表对象
table_names = inspector.get_table_names()
for table_name in table_names:
    columns = inspector.get_columns(table_name)
    primary_keys = get_primary_key(table_name)
    
    table_columns = [
        Column(column['name'], map_column_type(column['type']), primary_key=(column['name'] in primary_keys))
        for column in columns
    ]
    
    table = Table(table_name, metadata, *table_columns)
    tables[table_name] = table

    fkeys = inspector.get_foreign_keys(table_name)
    for fkey in fkeys:
        if 'referred_table' in fkey:
            foreign_keys.setdefault(fkey['referred_table'], []).append((table_name, fkey['constrained_columns'][0]))

# 开始处理数据和外键
session = Session()
for table_name in table_names:
    primary_key = get_primary_key(table_name)
    if not primary_key:
        print(f"Warning: Table '{table_name}' does not have a primary key. Documents will not have unique identifiers.")
        continue

    for row in session.query(tables[table_name]).all():
        doc = model_to_dict(row, inspector.get_columns(table_name))
        if primary_key:
            doc['_id'] = doc[primary_key[0]]
        doc = process_foreign_keys(doc, table_name, processed_docs)
        mongo_db[table_name].insert_one(doc)

# 关闭数据库连接
session.close()
mongo_client.close()

