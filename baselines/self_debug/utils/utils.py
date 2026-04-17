import json
import os

import demjson
import openai
from pymongo import MongoClient
from pymongo.errors import OperationFailure
import tiktoken

# 连接到MongoDB
mongo_client = MongoClient('mongodb://localhost:27017/')


def parse_mongo_value(value):
    """ Helper function to format MongoDB query values for the keyword expression. """
    fields = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "$ifNull":
                fields.add(v[0].strip("$"))

            elif k in ["$size" , "$toInt" , "$toDouble"]:
                if isinstance(v, str):
                    fields.add(v.strip("$"))
                elif isinstance(v, dict):
                    fields.update(parse_mongo_value(v))
            elif k == "$cond":
                if isinstance(v, list):
                    fields.update(parse_conditions(v[0]))
                else:

                    fields.update(parse_conditions(v['if']))
                    fields.update(parse_mongo_value(v['then']))
                    fields.update(parse_mongo_value(v['else']))
            else:
                fields.update(parse_conditions(v))
    elif isinstance(value, str) and not str(value).isdigit():
        fields.add(value.strip("$"))
    return fields


def parse_conditions(conditions):
    fields = set()
    for key, value in conditions.items():
        if key in ["$or" , "$and"] and isinstance(value, list):
            for v in value:
                for field, cond in v.items():
                    fields.add(field)
        elif isinstance(value, dict) and key != "$expr":
            fields.add(key)
        elif key == "$expr":
            for op, val in value.items():
                if isinstance(val, list):
                    if op not in ["$or" , "$and"] and len(val) == 2:
                        val_1 = set([val[0].strip("$")]) if "$" in val[0] else parse_mongo_value(val[0])
                        val_2 = set([val[1].strip("$")]) if "$" in val[1] else parse_mongo_value(val[1])
                        fields.update(val_1)
                        fields.update(val_2)
                    elif op in ["$or" , "$and"]:
                        for exp in val:
                            for k, v in exp.items():
                                val_1 = set([str(v[0]).strip("$")]) if "$" in str(v[0]) else parse_mongo_value(v[0])
                                val_2 = set([str(v[1]).strip("$")]) if "$" in str(v[1]) else parse_mongo_value(v[1])
                                fields.update(val_1)
                                fields.update(val_2)
                    else:
                        raise RuntimeError(f"Error in parsing $expr conditions. {op}:{val}")
                elif isinstance(val, dict):
                    for k, v in val.items():
                        if k not in ["$or" , "$and"] and len(v) == 2:
                            val_1 = set([str(v[0]).strip("$")]) if "$" in str(v[0]) else parse_mongo_value(v[0])
                            val_2 = set([str(v[1]).strip("$")]) if "$" in str(v[1]) else parse_mongo_value(v[1])
                            fields.update(val_1)
                            fields.update(val_2)
                        
        elif isinstance(value, list) and len(value) == 2:
            fields.update(parse_mongo_value(value[0]))
            fields.update(parse_mongo_value(value[1]))
        elif key == "$cond":
            fields.update(parse_mongo_value(value[0]))
        else:
            fields.add(key)
    return fields

def parse_find(query):
    fields = set()

    collection = query.split(".",2)[1]
    args_str = "[" + query.split(".find(", 1)[1].split(")", 1)[0].strip(";") + "]"
    args = demjson.decode(args_str)
    filter_dict = args[0]
    projection_dict = args[1]
    if ".sort(" in query:
        sort_str = query.rsplit(".sort(", 1)[1].split(")", 1)[0]
    else:
        sort_str = None
    
    filter_fields = parse_conditions(filter_dict)

    projection_fields = set()
    for k, v in projection_dict.items():
        k = str(k).strip("$")
        v = str(v).strip("$")
        if v in ["1", "0"]:
            projection_fields.add(k)
        else:
            projection_fields.add(k)
            projection_fields.add(v)
    
    sort_fields = set()
    if sort_str:
        try:
            sort_dict = demjson.decode(sort_str)
            for k, v in sort_dict.items():
                sort_fields.add(k)
        except demjson.JSONDecodeError:
            return demjson.JSONDecodeError("Invalid JSON in sort")
        
    
    fields.update(list(filter_fields))
    fields.update(list(projection_fields))
    fields.update(list(sort_fields))

    return fields

def parse_group(group_dict):
    """ Parse and format the $group stage, including nested structures. """
    fields = set()
    _id = group_dict.pop('_id', None)
    if isinstance(_id, str):
        fields.add(_id.strip("$"))
    elif isinstance(_id, dict):
        for k, v in _id.items():
            fields.add(v.strip("$"))
            fields.add(k.strip("$"))

    for k, v in group_dict.items():
        fields.add(k.strip("$"))

        for agg_op, agg_val in v.items():
            if isinstance(agg_val, dict):
                fields.update(parse_conditions(agg_val))
            else:
                agg_val = str(agg_val)
                if not agg_val.isdigit():
                    fields.add(agg_val.strip("$"))

    return fields

def parse_lookup(lookup_dict:dict):
    fields = {"fields":[], "foreign_collection":""}
    # 解析聚合管道中的$lookup阶段
    if "pipeline" not in lookup_dict:
        fields["foreign_collection"] = lookup_dict['from']
        fields["fields"].append(lookup_dict["foreignField"])
        fields['fields'].append(lookup_dict["localField"])
        fields['fields'].append(lookup_dict['as'])
    else:
        fields["foreign_collection"] = lookup_dict['from']
        fields['fields'].append(lookup_dict['as'])

        for alias, field in lookup_dict['let'].items():
            fields["fields"].append(alias)
            if isinstance(field, str):
                fields['fields'].append(field.strip("$"))
            else:
                fields['fields'].extend(list(parse_conditions(field)))
        
        sub_fields = parse_pipeline(pipeline=lookup_dict['pipeline'])
        fields['fields'].extend(list(sub_fields))

    return fields

def parse_pipeline(pipeline:list):
    fields = set()
    for op in pipeline:
        if '$unwind' in op:
            if isinstance(op['$unwind'], str):
                unwind_path = op['$unwind']
                
            else:
                unwind_path = op['$unwind']['path']
            fields.add(unwind_path.strip('$'))
        elif '$match' in op:
            match_fields = parse_conditions(op['$match'])
            fields.update(match_fields)
        if '$group' in op:
            group_fields = parse_group(op['$group'])
            fields.update(group_fields)
        elif '$sort' in op:
            for k, v in op['$sort'].items():
                fields.add(k)
        elif '$project' in op:
            for k, v in op['$project'].items():
                k = str(k).strip("$")
                fields.add(k)
                if isinstance(v, str):
                    v = str(v).strip("$")
                    if v not in ["1", "0"]:
                        fields.add(v)
                elif isinstance(v, dict):
                    fields.update(parse_mongo_value(v))

        elif '$lookup' in op:
            lookup_fields = parse_lookup(lookup_dict=op['$lookup'])

            fields.update(lookup_fields['fields'])
    
    return set(fields)

def parse_aggregate(query):
    collection = query.split(".aggregate(", 1)[0].split("db.", 1)[1]
    operations_str = query.split(".aggregate(", 1)[1].rsplit(")", 1)[0]
    
    try:
        operations = demjson.decode(operations_str)
    except demjson.JSONDecodeError:
        raise demjson.JSONDecodeError("Invalid JSON in aggregate operations")
    
    fields = parse_pipeline(pipeline=operations)
    
    # return f"METHOD AGGREGATE ON {collection} {steps}".strip()
    return fields

def extract_fields(MQL:str):
    if ".find" in MQL:
        fields = parse_find(MQL)
    else:
        fields = parse_aggregate(MQL)
    
    return list(fields)

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

def schemas_transform(db_id:str, flag="LLM", cols="all"):
    folder_path = "./mongodb_schema/"
    file_path = os.path.join(folder_path, db_id + ".json")

    with open(file_path, "r") as f:
        schemas_json = json.load(f)

    schemas_str = ""
    if cols == "all":
        cols = schemas_json.keys()
    else:
        cols = cols
    for collection, fields_type in schemas_json.items():
        if collection not in cols:
            continue
        if flag == "LLM":
            schemas_str += """#### Collection: {}
#### Fields: """.format(collection)
            schemas_str += dfs_dict(d=fields_type, prefix="") + "\n"
        else:
            schemas_str += f"# {collection}: "
            schemas_str += dfs_dict(d=fields_type, prefix="") + "\n"

    if flag == "LLM":
        schemas_str = f"### Database: {db_id}\n" + schemas_str
    return schemas_str.strip("\n")


def get_alias_fields(db_id:str, fields:list):
    folder_path = "./mongodb_schema/"
    file_path = os.path.join(folder_path, db_id + ".json")

    with open(file_path, "r") as f:
        schemas_json = json.load(f)

    schemas_str = ""
    db_fields = []
    fields_db = []
    fields_alias = []
    for collection, fields_type in schemas_json.items():

        db_fields.extend(dfs_dict_list(d=fields_type, prefix=""))
    for field in fields:
        if field in db_fields:
            fields_db.append(field)
        else:
            fields_alias.append(field)
    return fields_db, fields_alias

def dfs_dict_list(d, prefix=""):
    """
    使用深度优先搜索遍历嵌套字典。
    
    :param d: 嵌套字典
    :param prefix: 当前节点的键路径
    """
    fields = []
    for key, value in d.items():
        current_path = prefix
        if isinstance(value, list):
            current_path = prefix + f"{key}."
            # 如果值是字典，则递归进入该子字典
            for sub_d in value:
                fields.extend(dfs_dict_list(sub_d, current_path))
        else:
            fields.append(current_path + key)

    return fields

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
            current_path = prefix + f"{key}."
            # 如果值是字典，则递归进入该子字典
            for sub_d in value:
                fields += dfs_dict(sub_d, current_path)
        else:
            fields += current_path + key + ", "

    return fields[:-1]


def parse_query(query):
    # 解析MQL查询

    if 'find' in query:
        collection_name = query.split('.')[1]
        query_body = demjson.decode("[" + query.split('find(')[1].split(')')[0] + "]")
        query_filter, query_projection = query_body[0], query_body[1]
        
        # 解析可选的sort和limit
        sort = None
        limit = None
        pipeline = []

        if '.sort(' in query:
            sort_body = query.split('.sort(')[1].split(')')[0]
            sort = demjson.decode(sort_body)
        if '.limit(' in query:
            limit_body = query.split('.limit(')[1].split(')')[0]
            limit = int(limit_body)
        if query_filter != {}:
            pipeline.append({"$match":query_filter})
        if query_projection != {}:
            pipeline.append({"$project":query_projection})
        if sort is not None:
            pipeline.append({"$sort":sort})
        if limit is not None:
            pipeline.append({"$limit":limit})
        return 'find', collection_name, pipeline
    elif 'aggregate' in query:
        collection_name = query.split('.')[1]
        pipeline = query.split('aggregate(')[1].split(')')[0]
        pipeline = demjson.decode(pipeline)
        return 'aggregate', collection_name, pipeline

    
def get_collection(query):
    # 解析MQL查询
    col = []
    if 'find' in query:
        collection_name = query.split('.')[1]
        return [collection_name]
    elif 'aggregate' in query:
        collection_name = query.split('.')[1]
        col.append(collection_name)
        pipeline = query.split('aggregate(')[1].split(')')[0]
        pipeline = demjson.decode(pipeline)
        for stage_dict in pipeline:
            if "$lookup" in stage_dict:
                lookup_dict = stage_dict["$lookup"]
                col.append(lookup_dict['from'])
                col = list(set(col))
        return col

def execute_query(MQL, db_id):
    query_type, collection_name, pipeline = parse_query(MQL)
    collection = mongo_client[db_id][collection_name]

    if query_type == 'find':
        query_filter = query_projection = sort = limit = None
        for stage in pipeline:
            if "$match" in stage:
                query_filter = stage["$match"]
            elif "$project" in stage:
                query_projection = stage["$project"]
            elif "$sort" in stage:
                sort = stage["$sort"]
            elif "$limit" in stage:
                limit = stage["$limit"]
        cursor = collection.find(query_filter, query_projection)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        if limit:
            cursor = cursor.limit(limit)
        result = list(cursor)
    elif query_type == 'aggregate':
        result = list(collection.aggregate(pipeline))
    return result

    
def get_alias_fields(db_id:str, fields:list):
    folder_path = "./mongodb_schema/"
    file_path = os.path.join(folder_path, db_id + ".json")

    with open(file_path, "r") as f:
        schemas_json = json.load(f)

    db_fields = []
    fields_db = []
    fields_alias = []
    for collection, fields_type in schemas_json.items():

        db_fields.extend(dfs_dict_list(d=fields_type, prefix=""))

    db_fields_lower = [ f.lower() for f in db_fields]
    for field in fields:
        if field.lower() in db_fields_lower:
            index = db_fields_lower.index(field.lower())
            fields_db.append(db_fields[index])
        else:
            fields_alias.append(field)
    return fields_db, fields_alias

def dfs_dict_list(d, prefix=""):
    """
    使用深度优先搜索遍历嵌套字典。
    
    :param d: 嵌套字典
    :param prefix: 当前节点的键路径
    """
    fields = []
    for key, value in d.items():
        current_path = prefix
        if isinstance(value, list):
            current_path = prefix + f"{key}."
            # 如果值是字典，则递归进入该子字典
            for sub_d in value:
                fields.extend(dfs_dict_list(sub_d, current_path))
        else:
            fields.append(current_path + key)

    return fields

if __name__ == "__main__":
    db_id = "cre_Drama_Workshop_Groups"
    schemas = schemas_transform(db_id=db_id)
    print(schemas)

    # count tokens
    encoding = tiktoken.encoding_for_model("gpt-4")
    token_count = len(encoding.encode(schemas))
    print(f"Token count: {token_count}")