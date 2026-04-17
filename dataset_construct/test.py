import sqlite3
import json
from collections import defaultdict
import os
from tqdm import tqdm

def dfs(graph, start, visited=None, path=None):
    if visited is None:
        visited = set()
    if path is None:
        path = []
    
    visited.add(start)
    path.append(start)
    
    for next_node in graph[start]:
        if next_node not in visited:
            dfs(graph, next_node, visited, path)
    
    return visited, path

def get_larger_set(set_a, set_b):
    if set_a.issubset(set_b):
        return set_b
    elif set_b.issubset(set_a):
        return set_a
    else:
        return None

def find_set_path(graph):
    graph_set_paths = []
    graph_sets = []
    for start_node in graph.keys():
        visited, path = dfs(graph, start_node)
        
        flag = True
        for i in range(len(graph_sets)):
            new_set = get_larger_set(graph_sets[i], visited)
            if new_set:
                if new_set == visited:
                    for j in range(len(graph_set_paths)):
                        if set(graph_set_paths[j]['table_set']) == graph_sets[i]:
                            graph_set_paths[j]['start_table'] = start_node
                            graph_set_paths[j]['table_set'] = list(new_set)
                            graph_set_paths[j]['table_path'] = path
                    graph_sets[i] = new_set
                flag = False
                break
        
        if flag:
            graph_sets.append(visited)
            graph_set_paths.append({
                "start_table": start_node,
                "table_set": list(visited),
                "table_path": path
            })
        
        if not graph_sets:
            graph_sets.append(visited)
            graph_set_paths.append({
                "start_table": start_node,
                "table_set": list(visited),
                "table_path": path
            })
    
    return graph_sets, graph_set_paths

def build_graph(db_path):
    """构建表示表之间外键关系的图"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    graph = defaultdict(list)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    for table in tables:
        cursor.execute(f"PRAGMA foreign_key_list({table});")
        for row in cursor.fetchall():
            referenced_table = row[2]
            graph[table].extend([])
            graph[referenced_table].append(table)  # 假设关系是双向的
    conn.close()
    return graph



def get_all_table_names(db_path:str):
    conn = sqlite3.connect(db_path)

    # 创建一个游标对象
    cur = conn.cursor()

    # 执行SQL查询获取所有表名
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")

    # 获取查询结果
    tables = cur.fetchall()

    table_names = [ t[0] for t in tables ]

    conn.close()
    return table_names

def get_connected_tables_by_table(db_path:str, table_name:str):
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)

    # 创建一个游标对象
    cur = conn.cursor()

    # 执行SQL查询获取所有与给定表连接的表名
    query = f"""
    SELECT name
    FROM sqlite_master
    WHERE type='table'
    AND sql LIKE '%REFERENCES {table_name}%'
    AND name NOT LIKE 'sqlite_%';
    """

    cur.execute(query)

    # 获取查询结果
    connected_tables = cur.fetchall()

    connected_tables = [ct[0] for ct in connected_tables]

    conn.close()
    return connected_tables



def get_fk_by_table(db_path:str, table_name:str):
    '''
        获取单个表的外键信息，即遍历查看其他表是否外键引用该表
    '''

    fk_infos = []

    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)

    # 创建一个游标对象
    cur = conn.cursor()

    # connected_tables = get_connected_tables_by_table(db_path, table_name)
    tables_all = get_all_table_names(db_path)

    for table in tables_all:
        if table == table_name:
            continue
        fk_info = {"table_name":table, "from":[], "to":[]}
        # 执行PRAGMA查询来获取外键信息
        cur.execute("PRAGMA foreign_key_list({})".format(table))

        # 获取查询结果
        foreign_keys = cur.fetchall()

        flag = False
        for fk in foreign_keys:
            if fk[2] == table_name:
                flag = True
                fk_info['from'].append(fk[3])
                fk_info['to'].append(fk[4])
        if flag:
            fk_infos.append(fk_info)

    conn.close()
    return fk_infos


def get_fk_info(db_path:str):
    '''
fk_infos_all = {
    "table1":[
        {
            # table2是具有外键的表
            # 外键从table2连接到table1
            "table_name": "table2",
            "from":["column21", "column22"],
            "to":["column11", "column12"]
        }
    ]
}
    '''
    fk_infos_all = {}
    table_names = get_all_table_names(db_path)

    for table_name in table_names:
        fk_infos = get_fk_by_table(db_path, table_name)

        fk_infos_all[table_name] = fk_infos
    

    return fk_infos_all

def get_primary_key(db_path, table_name):
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()


    # 执行PRAGMA查询来获取表的信息
    cursor.execute(f"PRAGMA table_info({table_name})")

    # 获取查询结果
    table_info = cursor.fetchall()

    primary_keys = [column[1] for column in table_info if column[5] > 0]  # 如果是主键，则第五个字段为1

    conn.close()
    return primary_keys

def get_all_data(db_path, table_name:str, n=-1):
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 获取表的所有列名
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns_info = cursor.fetchall()

    # 提取列名
    columns = [col_info[1] for col_info in columns_info]

    # 查询表的所有数据
    if n == -1:
        cursor.execute(f"SELECT * FROM {table_name}")
    else:
        cursor.execute(f"SELECT * FROM {table_name} LIMIT {n}")


    rows = cursor.fetchall()

    # 将行数据与列名对应，创建一个新的列表
    data_list = [dict(zip(columns, row)) for row in rows]
    conn.close()
    return data_list

def get_column_type(db_path, table_name:str, column_name):
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 执行PRAGMA查询来获取表的信息
    cursor.execute(f"PRAGMA table_info({table_name})")

    # 获取查询结果
    table_info = cursor.fetchall()

    # 查找指定列的数据类型
    column_type = None
    for column in table_info:
        if column[1] == column_name:  # 如果列名匹配，则第一个字段为列名
            column_type = column[2]  # 数据类型为第三个字段
            break

    # 关闭数据库连接
    conn.close()

    return column_type

def get_data_by_column_value(db_path, table_name:str, column_names, values):

    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 获取表的所有列名
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns_info = cursor.fetchall()

    # 提取列名
    columns = [col_info[1] for col_info in columns_info]

    if len(column_names) == 1:
        column_name = column_names[0]
        value = values[0]
        # 查询表的所有数据
        column_type = get_column_type(db_path, table_name, column_name)

        if "char" in column_type.lower() or "text" in column_type.lower() or "date" in column_type.lower() :
            cursor.execute(f"SELECT * FROM {table_name} WHERE {column_name} = '{value}'")
        else:
            cursor.execute(f"SELECT * FROM {table_name} WHERE {column_name} = {value}")

    else:
        sql = f"SELECT * FROM {table_name} WHERE "
        for column_name, value in zip(column_names, values):
            column_type = get_column_type(db_path, table_name, column_name)
            # print(column_type)

            if "char" in column_type.lower() or "text" in column_type.lower() or "date" in column_type.lower() :
                sql += f"{column_name} = '{value}' AND "
            else:
                sql += f"{column_name} = {value} AND "

        sql = sql[:-5] + ";"
        # print(sql)
        cursor.execute(sql)
    rows = cursor.fetchall()

    # 将行数据与列名对应，创建一个新的列表
    data_list = [dict(zip(columns, row)) for row in rows]

    conn.close()
    return data_list

def get_data_by_table(db_path, table_name:str, column_name, value, n=-1):

    # print(table_name)
    if column_name and value:
        datas = get_data_by_column_value(db_path, table_name, column_name, value)
    else:
        datas = get_all_data(db_path, table_name, n)

    fks = fk_info_all[table_name]

    # print("fk:\n", fks)

    for i in range(len(datas)):
        data = datas[i]


        for fk in fks:
            # print(fk)
            fk_table_name = fk['table_name']
            fk_column_from = fk['from']
            fk_column_to = fk['to']

            try:
                value = [data[column] for column in fk_column_to]
            except:
                value = [data[column] for column in fk_column_from]
            # del data[fk_column_to]
            data[fk_table_name] = get_data_by_table(db_path, fk_table_name, fk_column_from, value)

    return datas

def get_column_type(db_path, table_name:str, column_name):
    # 连接到SQLite数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 执行PRAGMA查询来获取表的信息
    cursor.execute(f"PRAGMA table_info({table_name})")

    # 获取查询结果
    table_info = cursor.fetchall()

    # 查找指定列的数据类型
    column_type = None
    for column in table_info:
        if column[1] == column_name:  # 如果列名匹配，则第一个字段为列名
            column_type = column[2]  # 数据类型为第三个字段
            break

    # 关闭数据库连接
    conn.close()

    return column_type

def get_all_types(db_path, table_name):
    # 连接到数据库
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 查询数据表的列信息
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns_info = cursor.fetchall()

    # 创建一个字典来保存列名和数据类型
    columns_types = {}

    # 遍历查询结果，填充字典
    for column_info in columns_info:
        columns_types[column_info[1]] = column_info[2]  # 列名作为键，数据类型作为值

    conn.close()
    return columns_types

def get_schemas_by_table(db_path, table_name:str, ret_list=False):


    types = get_all_types(db_path, table_name)

    fks = fk_info_all[table_name]

    for fk in fks:
        fk_table_name = fk['table_name']
        # print(f"{table_name} -> {fk_table_name}")
        types[fk_table_name] = get_schemas_by_table(db_path, fk_table_name, ret_list=True)
    if ret_list:
        return [types]
    else:
        return types


# 执行流程
db_name = "soccer_1"
db_path = f"./spider/spider/database/{db_name}/{db_name}.sqlite"


fk_info_all = get_fk_info(db_path)


with open("./test/fk_info.json", "w") as f:
    json.dump(fk_info_all, f, indent=4)
graph_table = build_graph(db_path)
graph_set, set_paths = find_set_path(graph_table)

with open("./test/set_paths.json", "w") as f:
    json.dump(set_paths, f, indent=4)

data_all = {}
schemas_all = {}
for set_path in set_paths:
    start_table = set_path['start_table']
    # data  = get_data_by_table(db_path, start_table, None, None)
    schemas = get_schemas_by_table(db_path, start_table)

    # data_all[start_table] = data
    schemas_all[start_table] = schemas


# with open("./test/data.json", "w") as f:
#     json.dump(data_all, f, indent=4)

with open("./test/schemas.json", "w") as f:
    json.dump(schemas_all, f, indent=4)