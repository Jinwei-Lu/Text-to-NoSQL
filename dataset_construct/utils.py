
import openai
import random
import json
import re
import os
import sqlite3
from pymongo import MongoClient
import demjson
from deepdiff import DeepDiff
import decimal
import requests
import random
import numpy as np

random.seed(1234)
np.random.seed(1234)


api_base = "https://openkey.cloud/v1"
# api_base = "https://openkey.blue/v1"

# 替换为你的OpenAI API密钥
api_key = "sk-vaxsS9HAmEnogXxf095d4173F2C9401bB55721994c3d7942"

random.seed(1234)

client = openai.Client(api_key = api_key, base_url=api_base)

API_BASE = "https://api.lingyiwanwu.com/v1"
API_KEY = "5c6bcdf8c0f64668bfe35408a3fe6115"
client_Yi = openai.OpenAI(
  api_key=API_KEY,
  base_url=API_BASE
)




def get_query_from_reply(raw_rely:str):
    try:
        code = "db." + raw_rely.rsplit("```", 2)[1].split("db.", 1)[1]

        db_count = code.count("db.")

        if db_count > 1:
            code = "db." + code.rsplit("db.", 1)[1]
    except:
        code = "db." + raw_rely.split("db.", 1)[1].split(")", 1)[0] + ")"

    code = code.replace(".pretty()", "")
    return code

def convert_regex_query(query: str):
    """
    将MongoDB查询字符串中的正则表达式语法转换为pymongo中使用的格式，
    同时确保筛选出的正则表达式中的项不包括数字。
    例如，将"{ $not: /M/ }"转换为{"$not": {"$regex": "M"}}。
    """
    # 查找所有的正则表达式模式
    regex_patterns =  re.findall(r'/(.*?)/', query)
    
    # print("patterns:\n", regex_patterns)
    # 筛选出不包含数字的正则表达式模式
    non_digit_patterns = [pattern for pattern in regex_patterns if not re.search(r'\d', pattern)]
    
    # 遍历符合条件的模式，并替换为pymongo兼容的格式
    for pattern in non_digit_patterns:
        # 构建pymongo兼容的正则表达式字符串
        pymongo_regex = '{"$regex": "' + pattern + '"}'
        
        # 替换原始查询字符串中的正则表达式部分
        query = query.replace('/' + pattern + '/', pymongo_regex)
    
    # print("query:\n", query)
    return query

def remove_ann(query:str):
    query_lines = query.split("\n")
    query_lines_no_ann = []
    for line in query_lines:
        if """//""" in line:
            line = line.split("//")[0]
        query_lines_no_ann.append(line)

    query_with_no_ann = "\n".join(query_lines_no_ann)
    # print("query_with_no_ann:\n", query_with_no_ann)
    return query_with_no_ann

def deal_query(query:str):
    # print(query)
    # query = query.replace(" $ ", "$").replace(" . ", ".").replace(" ( [", "([").replace(" : ", ":")
    # query = query.replace(" ", "")
    # print("query:\n", query)
    method = query.split("(", 1)[0].rsplit(".", 1)[1]
    if method == "find":
        args = "[" + query.split("(", 1)[1].split(")", 1)[0] + "]"
    else:
        args = query.split("(", 1)[1].rsplit(")", 1)[0]

    # try:
    #     query_json = json.dumps(demjson.decode(args))
    # except:
    #     query_json = json.dumps(demjson.decode("[" + args + "]"))
    
    # print("args:\n", args)

    arguments = convert_regex_query(remove_ann(args))
    # print("args:\n", arguments)
    arguments = json.dumps(demjson.decode(arguments), indent=2)

    # print("arguments:\n", arguments)

    return arguments


def execute_sql(db_name:str, ref_sql:str):
    """
    Executes a SQL query on a specified SQLite database and returns 
    the results along with the schema.

    Args:
        db_name (str): The name of the database.
        ref_sql (str): The SQL query to be executed.

    Returns:
        dict: Contains the column names as 'schemas' and query results as 'results'.
    """

    tokens = ref_sql.split()

    ref = {}
    for id, token in enumerate(tokens):
        if token.lower() == "as":
            ref[tokens[id + 1]] = tokens[id - 1]

    db_path = f"./spider/spider/database/{db_name}/{db_name}.sqlite"
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)

    cur = conn.cursor()

    # 执行一个查询语句
    cur.execute(ref_sql)

    # 获取查询结果
    rows = cur.fetchall()

    # 获取列名
    column_names = [desc[0].replace(".","_").replace("(", "_").replace(")", "").replace("_*", "").replace(" ", "_") for desc in cur.description]

    for i in range(len(column_names)):
        for name, table in ref.items():
            column_names[i] = column_names[i].replace(name, table)

    # 打印查询结果
    results = []
    for row in rows:
        row = list(row)
        for i in range(len(row)):
            if isinstance(row[i], float):
                row[i] = round(row[i], 4)
        results.append(dict(zip(column_names, row)))

    # 关闭游标和连接
    cur.close()
    conn.close()

    return {"schemas": column_names, "results": results}

def excute_mongodb_query(query:str, db_name:str):
    # 连接到MongoDB服务器
    client = MongoClient('mongodb://localhost:27017')

    # 选择数据库和集合
    db = client[db_name]

    def replace_spaces(match):
        # Replace spaces with a placeholder (e.g., "___")
        return match.group(0).replace(' ', '___')
    
    # Use a non-greedy regex to match text within quotes
    text_with_placeholders = re.sub(r'"(.*?)"', replace_spaces, query)
    
    # Step 3: Remove all spaces
    text_without_spaces = text_with_placeholders.replace(' ', '')
    
    # Step 4: Replace placeholders back with spaces
    query = text_without_spaces.replace('___', ' ').replace("\" ", "\"").replace(" \"", "\"").replace(" : ", ":")

    while " $" in query:
        query = query.replace(" $", "$")

    while "$ " in query:
        query = query.replace("$ ", "$")

    while " ." in query:
        query = query.replace(" .", ".")

    while ". " in query:
        query = query.replace(". ", ".")

    query = query.replace("Computer Info.Systems", "Computer Info. Systems").replace("St.Helena", "St. Helena").replace("Comp.Sci.", "Comp. Sci.").replace("{", " { ").replace("}", " } ").replace(",", " , ")
    # print("\n query:\n", query)
    

    collection_name = query.split(".", 2)[1]
    method = query.split("(", 1)[0].rsplit(".", 1)[1]

    if method == "distinct":
        raise demjson.JSONDecodeError("Method Error: need a list of dict")

    method_last = query.split("(", 1)[1].split(")", 1)[1]
    if "." in method_last:
        args = method_last.split("(", 1)[1].split(")", 1)[0]
        args = json.dumps(demjson.decode(args))

        method_last = method_last.split("(", 1)[0] + "(" + args.replace("{", "").replace("}", "").replace(":", ",") + ")"
    else:
        method_last = ""


    arguments = deal_query(query)

#     print(f"""query: {query}
# collection: {collection_name}
# method: {method}
# method_last: {method_last}
# args: {arguments}
# """)
    # print("\n\n", arguments)
    
    collection = db[collection_name]

    if method == "aggregate":
        arguments = json.loads(arguments)
        code_str = f"collection.{method}(arguments){method_last}"
        results = eval(code_str)
    else:
        arguments = json.loads(arguments)
        q = arguments[0]
        p = arguments[-1]
        code_str = f"""collection.{method}(q, p){method_last}"""
        # print("code_str:\n", code_str)
        results = eval(code_str)
    # try:
    #     code_str = f"collection.{method}(json.loads(arguments)){method_last}"
    #     results = eval(code_str)
    
    # except:
    #     arguments = json.loads(arguments)
    #     q = arguments[0]
    #     p = arguments[-1]
    #     code_str = f"""collection.{method}(q, p){method_last}"""
    #     # print("code_str:\n", code_str)
    #     results = eval(code_str)
    # print("code_str:\n", code_str)
    results_final = []
    for result in results:
        for key, value in result.items():
            if isinstance(value, float):
                result[key] = round(value, 4)
        results_final.append(result)

    return results_final


def generate_reply(messages, model = "gpt-3.5-turbo-0125", n=1, temperature=0.0):
    
    # print("generate...")
    completions = client.chat.completions.create(
        model=model,
        messages=messages,
        n = n,
        # stream = False,
        temperature=temperature
    )
    # print(completions)
    if n == 1:
        mes = completions.choices[0].message.content
    else:
        mes = [completions.choices[i].message.content for i in range(n)]
    return mes

def check_dict_values_not_collection(d:dict):
    for value in d.values():
        if isinstance(value, (dict, list)):
            return False
    return True



def clean_query(text:str):
    # Step 1 & 2: Replace spaces within quotes with a placeholder
    # def replace_spaces(match):
    #     # Replace spaces with a placeholder (e.g., "___")
    #     return match.group(0).replace(' ', '___')
    
    # # Use a non-greedy regex to match text within quotes
    # text_with_placeholders = re.sub(r'"(.*?)"', replace_spaces, text)
    
    # # Step 3: Remove all spaces
    # text_without_spaces = text_with_placeholders.replace(' ', '')
    
    # # Step 4: Replace placeholders back with spaces
    # final_text = text_without_spaces.replace('___', ' ')
    characters = "().,:[]{}$\""
    # pattern = r'([().,:[]{}])'
    
    # 使用re.sub函数进行替换，对于每一个匹配到的字符，用其本身前后各加一个空格进行替换
    # final_text = re.sub(pattern, r' \1 ', text)
    pattern = f"([{re.escape(characters)}])"
    # 使用正则表达式替换，确保字符的两侧都有空格
    final_text = re.sub(pattern, r' \1 ', text)

    final_text = final_text.replace("\n", "")
    
    final_text = " ".join(final_text.split())

    return final_text

def get_nlq_from_raw_reply(reply:str):
    nlq = reply
    if "```plaintext" in nlq:
        nlq = nlq.split("```plaintext", 1)[1].split("```", 1)[0].replace("\n", "")
    elif "```" in nlq:
        nlq = nlq.split("```", 2)[1].replace("\n", "")

    return nlq


def generate_claude_reply(messages:list, model:str):
    url = "https://openkey.cloud/v1/chat/completions"

    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer sk-vaxsS9HAmEnogXxf095d4173F2C9401bB55721994c3d7942'
    }

    data = {
        "model": model,
        "messages": messages,
    }

    response = requests.post(url, headers=headers, json=data)

    response_new = response.json()['choices'][0]['message']['content']

    return response_new

def generate_Yi_reply(messages:list, model:str):
    completion = client_Yi.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        frequency_penalty=-0.1,
        presence_penalty=-0.1

    )

    return completion.choices[0].message.content

def check(query:str, db_name:str, ref_sql:str, need_print=True):

    results_sql = execute_sql(db_name, ref_sql)['results']
    if need_print:
        print("SQL:\n", results_sql, "\nResults' Length:\n", len(results_sql), "\n")

    try:
        results_mql = excute_mongodb_query(query, db_name)
    except Exception as ex:
        if need_print:
            print("Exception:\n", ex)
            print("Exception type:\n",type(ex))
        return {"match":False, "Info":f"{ex}"}


    if need_print:
        print("MQL:\n", results_mql, "\nResults' Length:\n", len(results_mql))
    

    cmp = DeepDiff(results_mql, results_sql, ignore_order=True)
    if not cmp:
        return {"match":True, "Info":""}
    else:
        info = ""
        # if len(results_sql) == len(results_mql):
        #     info = "Execute Results' Length Equal"
        '''
            1、empty dict
            1、执行结果键名不满足用户要求
                1) 纯粹键名错误
                2) 返回了_id
            2、返回结果为字符串列表(非字典)
            3、类型转换异常
            4、大多情况下是确实错误了，这些需要分好类
                1) 长度相同但值错误 (找错了数据)
                2) 长度都是错的 (filter错误)
            5、无法执行
        '''
        if len(results_mql) == 0: 
            info = "filter wrong and result is empty"
        elif len(results_sql) == 0:
            info = "sql result is empty"
        elif not results_mql[0].keys():
            info = "dict is empty"
        elif not set(results_sql[0].keys()) == set(results_mql[0].keys()):
            info = "key name wrong"
            if "_id" in results_mql[0].keys():
                info += " and don't need _id"
        elif not check_dict_values_not_collection(results_mql[0]):
            info = "nested structure"
        elif len(results_sql) == len(results_mql):
            info = "value wrong"
        else:
            info = "filter wrong"
        return {"match":False, "Info":info}

def schemas_transform(db_id:str):
    folder_path = "./mongodb_schema/"
    file_path = os.path.join(folder_path, db_id + ".json")

    with open(file_path, "r") as f:
        schemas_json = json.load(f)

    schemas_str = f"## MongoDB Collections with their Fields in {db_id} Database\n"
    for collection, fields_type in schemas_json.items():
        fields_list = dfs_dict_list(d=fields_type, prefix="")
        fields = ", ".join(fields_list)
        schemas_str += f"""### {collection}: {fields}""" + "\n"
    return schemas_str.strip("\n")

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
    db_id = "hr_1"

    print(schemas_transform(db_id=db_id))
    # data_save_path = "./text2MongoDB_dataset/MQSpider_final.json"
    # # data_save_path = "./text2MongoDB_dataset/MQSpider_final_wrong.json"
    # with open(data_save_path, "r") as f:
    #     data_all = json.load(f)

    # # [438,488,779,1400,1503,2026,2154,4067]
    # id = 4067

    # data = [ d for d in data_all if d['record_id'] == id][0]

    # query = data['MQL']
    # print("MQL:\n", query.replace("\n", " "), "\n")

    # query_clean = clean_query(query)
    # print("MQL_clean:\n", query)
    # ref_sql = data['ref_sql']
    # db_name = data['db_id']
    # # info = check(query, db_name, ref_sql, need_print=True)
    # # print(info)
    # print(excute_mongodb_query(query=query_clean, db_name=db_name))
    # print(excute_mongodb_query(query=query, db_name=db_name))

    # query = "db.Owners.aggregate([\n  {\n    $unwind: \"$Dogs\"\n  },\n  {\n    $unwind: \"$Dogs.Treatments\"\n  },\n  {\n    $group: {\n      _id: \"$owner_id\",\n      total_cost: {\n        $sum: \"$Dogs.Treatments.cost_of_treatment\"\n      },\n      zip_code: {\n        $first: \"$zip_code\"\n      }\n    }\n  },\n  {\n    $sort: {\n      total_cost: -1\n    }\n  },\n  {\n    $limit: 1\n  },\n  {\n    $project: {\n      owner_id: \"$_id\",\n      zip_code: 1,\n      _id: 0\n    }\n  }\n]);\n"

    # print("query_ori:\n", query.replace("\n", ""), "\n")

    # query_clean = clean_query(query)
    # print("\nquery_clean:\n", query_clean, "\n")
    # results = excute_mongodb_query(query_clean, "dog_kennels")

    # if results != []:
    #     print("results:\n", json.dumps(results, indent=2), "\n")
    # else:
    #     print("results:\n", json.dumps(results, indent=2), "\n")
