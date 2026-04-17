"""
MongoDB Field Parser
用于解析MongoDB查询中的数据库字段

This module extracts and lists all fields used in MongoDB queries,
including find queries, aggregation pipelines, and complex expressions.
"""

import json
import demjson
from typing import List, Set, Dict, Union, Any

class MongoFieldParser:
    def __init__(self):
        # 存储每个集合的字段
        self.collection_fields = {}
        self.current_collection = None
        # 存储计算字段和临时字段
        self.computed_fields = set()
        
    def parse_query(self, query: str) -> Dict[str, List[str]]:
        """
        解析MongoDB查询语句，提取所有字段
        
        Args:
            query (str): MongoDB查询语句
            
        Returns:
            Dict[str, List[str]]: 每个集合的字段列表
        """
        # 提取集合名
        try:
            collection = query.split("db.", 1)[1].split(".", 1)[0]
            self.current_collection = collection
            
            if collection not in self.collection_fields:
                self.collection_fields[collection] = set()
        except Exception as e:
            print(f"Error extracting collection name: {e}")
            return {}
        
        if ".find(" in query:
            self._parse_find_query(query)
        elif ".aggregate(" in query:
            self._parse_aggregate_query(query)
            
        # 返回每个集合的排序后的字段列表
        return {coll: sorted(list(fields)) 
                for coll, fields in self.collection_fields.items()}
    
    def _add_field(self, field: str, collection: str = None) -> None:
        """添加字段到指定集合"""
        if collection is None:
            collection = self.current_collection
            
        # 检查是否应该排除该字段
        if self._should_exclude_field(field):
            return
            
        if collection not in self.collection_fields:
            self.collection_fields[collection] = set()
        self.collection_fields[collection].add(field)
    
    def _should_exclude_field(self, field: str) -> bool:
        """检查是否应该排除该字段"""
        # 排除以下字段：
        # 1. 以 $$ 开头的变量引用
        # 2. _id 的子字段（在group阶段中）
        # 3. lookup的as字段
        # 4. 计算字段
        return (field.startswith("$$") or
                field.startswith("_id.") or
                field in self.computed_fields)

    def _add_computed_field(self, field: str) -> None:
        """添加计算字段"""
        self.computed_fields.add(field)
        
    def _parse_find_query(self, query: str) -> None:
        """解析find查询中的字段"""
        try:
            # 提取find()中的参数
            args_str = query.split(".find(", 1)[1].rsplit(")", 1)[0].strip()
            
            # 处理参数字符串
            args_str = args_str.replace("'", '"')  # 将单引号替换为双引号
            args_str = args_str.replace("None", "null")  # 处理Python的None
            args_str = args_str.replace("True", "true").replace("False", "false")  # 处理布尔值
            
            # 确保args_str是一个有效的数组
            if not args_str.startswith("["):
                args_str = "[" + args_str + "]"
                
            try:
                args = demjson.decode(args_str)
            except:
                # 如果解析失败，尝试分割参数
                parts = args_str.split("}, {")
                if len(parts) > 1:
                    # 重建参数字符串
                    args_str = "[{"
                    args_str += "}, {".join(parts)
                    args_str += "}]"
                    args = demjson.decode(args_str)
                else:
                    raise
                    
            if args and isinstance(args[0], dict):
                self._extract_fields_from_dict(args[0])
                
            # 处理投影字段 (第二个参数)
            if len(args) > 1 and isinstance(args[1], dict):
                self._extract_projection_fields(args[1])
        except Exception as e:
            print(f"Error parsing find query: {e}")
    
    def _parse_aggregate_query(self, query: str) -> None:
        """解析aggregate查询中的字段"""
        try:
            # 提取aggregate()中的参数
            pipeline_str = query.split(".aggregate(", 1)[1].rsplit(")", 1)[0].strip()
            
            # 处理参数字符串
            pipeline_str = pipeline_str.replace("'", '"')  # 将单引号替换为双引号
            pipeline_str = pipeline_str.replace("None", "null")  # 处理Python的None
            pipeline_str = pipeline_str.replace("True", "true").replace("False", "false")  # 处理布尔值
            
            # 尝试直接解析
            try:
                pipeline = demjson.decode(pipeline_str)
            except:
                # 如果解析失败，尝试规范化MongoDB操作符
                pipeline_str = pipeline_str.replace("$match:", '"$match":')
                pipeline_str = pipeline_str.replace("$group:", '"$group":')
                pipeline_str = pipeline_str.replace("$project:", '"$project":')
                pipeline_str = pipeline_str.replace("$lookup:", '"$lookup":')
                pipeline_str = pipeline_str.replace("$unwind:", '"$unwind":')
                pipeline_str = pipeline_str.replace("$sort:", '"$sort":')
                pipeline_str = pipeline_str.replace("from:", '"from":')
                pipeline_str = pipeline_str.replace("localField:", '"localField":')
                pipeline_str = pipeline_str.replace("foreignField:", '"foreignField":')
                pipeline_str = pipeline_str.replace("as:", '"as":')
                pipeline_str = pipeline_str.replace("pipeline:", '"pipeline":')
                pipeline = demjson.decode(pipeline_str)
                
            if isinstance(pipeline, list):
                for stage in pipeline:
                    if isinstance(stage, dict):
                        self._parse_aggregate_stage(stage)
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")
    
    def _parse_aggregate_stage(self, stage: Dict) -> None:
        """解析聚合管道的每个阶段"""
        if not isinstance(stage, dict):
            return
            
        for operator, value in stage.items():
            if operator == "$match":
                if "$expr" in value:
                    self._handle_expr_operator(value["$expr"])
                else:
                    self._extract_fields_from_dict(value)
            elif operator == "$group":
                self._extract_group_fields(value)
            elif operator == "$project":
                self._extract_projection_fields(value)
            elif operator == "$lookup":
                self._extract_lookup_fields(value)
            elif operator == "$unwind":
                # 处理$unwind操作
                if isinstance(value, str):
                    self._add_field(value.strip("$"))
                elif isinstance(value, dict) and "path" in value:
                    self._add_field(value["path"].strip("$"))
            elif operator == "$sort":
                # 处理$sort操作
                if isinstance(value, dict):
                    for field in value.keys():
                        if not field.startswith("$"):
                            self._add_field(field)
            elif operator in ["$count", "$limit", "$skip"]:
                pass
    
    def _handle_expr_operator(self, expr_value: dict) -> None:
        """处理$expr操作符"""
        if isinstance(expr_value, dict):
            for op, value in expr_value.items():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.startswith("$"):
                            if not self._should_exclude_field(item):
                                self._add_field(item.strip("$"))
                        elif isinstance(item, dict):
                            self._extract_fields_from_dict(item)
                elif isinstance(value, str) and value.startswith("$"):
                    if not self._should_exclude_field(value):
                        self._add_field(value.strip("$"))
                elif isinstance(value, dict):
                    self._extract_fields_from_dict(value)
    
    def _extract_fields_from_dict(self, d: Dict, parent: str = "") -> None:
        """递归提取字典中的字段名"""
        if not isinstance(d, dict):
            return
            
        for key, value in d.items():
            if key == "$expr":
                self._handle_expr_operator(value)
                continue
            elif key.startswith("$"):
                continue
                
            full_key = f"{parent}.{key}" if parent else key
            
            if isinstance(value, dict):
                if all(k.startswith("$") for k in value.keys()):
                    self._add_field(full_key)
                self._extract_fields_from_dict(value, full_key)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._extract_fields_from_dict(item, full_key)
            else:
                self._add_field(full_key)
    
    def _extract_group_fields(self, group_dict: Dict) -> None:
        """提取$group阶段中的字段"""
        for key, value in group_dict.items():
            # 将所有group阶段的输出字段标记为计算字段
            if key != "_id":
                self._add_computed_field(key)
                
            if key == "_id":
                if isinstance(value, str) and value.startswith("$"):
                    self._add_field(value[1:])
                elif isinstance(value, dict):
                    for _, field in value.items():
                        if isinstance(field, str) and field.startswith("$"):
                            self._add_field(field[1:])
            elif isinstance(value, dict):
                for op, field in value.items():
                    if isinstance(field, str) and field.startswith("$"):
                        self._add_field(field[1:])
    
    def _extract_projection_fields(self, proj_dict: Dict) -> None:
        """提取投影中的字段"""
        for key, value in proj_dict.items():
            if not key.startswith("$"):
                if isinstance(value, (int, bool)) and value:
                    # 简单投影
                    self._add_field(key)
                elif isinstance(value, str) and value.startswith("$"):
                    # 字段引用
                    self._add_field(value[1:])
                elif isinstance(value, dict):
                    # 将使用表达式的字段标记为计算字段
                    self._add_computed_field(key)
                    for op, field in value.items():
                        if isinstance(field, str) and field.startswith("$"):
                            self._add_field(field[1:])
                            
    def _extract_lookup_fields(self, lookup_dict: Dict) -> None:
        """提取$lookup阶段中的字段"""
        if not isinstance(lookup_dict, dict):
            return
            
        # 记录原始集合
        original_collection = self.current_collection
            
        # 将as字段标记为计算字段
        if "as" in lookup_dict:
            self._add_computed_field(lookup_dict["as"])
            
        if "localField" in lookup_dict:
            self._add_field(lookup_dict["localField"])
        if "foreignField" in lookup_dict and "from" in lookup_dict:
            self._add_field(lookup_dict["foreignField"], lookup_dict["from"])
            
        if "pipeline" in lookup_dict and "from" in lookup_dict:
            self.current_collection = lookup_dict["from"]
            pipeline = lookup_dict["pipeline"]
            if isinstance(pipeline, list):
                for stage in pipeline:
                    if isinstance(stage, dict):
                        self._parse_aggregate_stage(stage)
                        
        # 恢复原始集合
        self.current_collection = original_collection

def main():
    """测试示例"""
    parser = MongoFieldParser()
    
    # file_path = "./data/train/TEND_train.json"
    # db_schema_path = "./mongodb_data/{db_id}.json"
    # with open(file_path, "r") as f:
    #     data = json.load(f)
    # record_id = 666
    # sample = [example for example in data if example['record_id'] == record_id][0]
    # db_id = sample['db_id']
    # query = sample['MQL']
    query = "db.Customers.aggregate([\n  {\n    $unwind: \"$Customer_Addresses\"\n  },\n  {\n    $lookup: {\n      from: \"Addresses\",\n      localField: \"Customer_Addresses.address_id\",\n      foreignField: \"address_id\",\n      as: \"Docs1\"\n    }\n  },\n  {\n    $unwind: \"$Docs1\"\n  },\n  {\n    $match: {\n      \"Docs1.city\": \"Lake Geovannyton\"\n    }\n  },\n  {\n    $group: {\n      _id: null,\n      count: { $sum: 1 }\n    }\n  },\n  {\n    $project: {\n      _id: 0,\n      count: 1\n    }\n  }\n]);\n"

    print("Query:", query)
    fields = parser.parse_query(query)
    print("Fields:", json.dumps(fields, indent=4))

if __name__ == "__main__":
    main()
