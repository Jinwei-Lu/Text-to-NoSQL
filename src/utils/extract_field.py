import re

class MongoFieldParser:
    def __init__(self):
        self.fields = set()
        
    def _parse_mongodb_query(self, query_str: str) -> dict:
        """使用正则表达式解析MongoDB查询字符串"""
        try:
            # MongoDB操作符列表
            operators = {
                # 比较操作符
                "eq", "gt", "gte", "in", "lt", "lte", "ne", "nin",
                # 逻辑操作符
                "and", "not", "nor", "or",
                # 元素操作符
                "exists", "type",
                # 评估操作符
                "expr", "jsonSchema", "mod", "regex", "text", "where",
                # 地理空间操作符
                "geoIntersects", "geoWithin", "near", "nearSphere",
                # 数组操作符
                "all", "elemMatch", "size",
                # 按位操作符
                "bitsAllClear", "bitsAllSet", "bitsAnyClear", "bitsAnySet",
                # 聚合操作符
                "sum", "avg", "first", "last", "max", "min", "push", "addToSet",
                # 聚合阶段操作符
                "group", "match", "project", "limit", "skip", "sort", "unwind",
                # 聚合阶段参数
                "from", "localField", "foreignField", "as", "pipeline",
                # 计数和控制字段
                "count", "size",
                # 其他操作符
                "comment", "rand"
            }
            
            # 提取字段名（包括带引号和不带引号的）
            fields = set()
            
            # 匹配带引号的字段名
            quoted_fields = re.finditer(r'"([^"$][^"]*)":', query_str)
            for match in quoted_fields:
                field = match.group(1)
                if field not in operators and not field.startswith("$"):
                    self._add_normalized_field(fields, field)
            
            # 匹配不带引号的字段名
            unquoted_fields = re.finditer(r'([a-zA-Z_$][\w$]*)\s*:', query_str)
            for match in unquoted_fields:
                field = match.group(1)
                if not field.startswith("$") and field not in operators:
                    self._add_normalized_field(fields, field)
            
            # 匹配字段引用（$field）
            field_refs = re.finditer(r'\$([a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*)', query_str)
            for match in field_refs:
                field = match.group(1)
                if not any(part in operators for part in field.split('.')):
                    self._add_normalized_field(fields, field)
            
            return {"fields": sorted(list(fields))}
        except Exception as e:
            print(f"Error extracting fields: {e}")
            return None
            
    def _add_normalized_field(self, fields: set, field: str) -> None:
        """添加规范化的字段名到集合中"""
        # 分割字段路径
        parts = field.split('.')
        
        # 如果字段路径中包含 "employees" 或 "employee"，提取最后的字段名
        if any(part in ["employees", "employee"] for part in parts[:-1]):
            field = parts[-1]
        
        # 添加字段名（如果不是数字且不是特殊字段）
        if not field.isdigit() and field not in ["employees", "employee", "job_history"]:
            fields.add(field)

    def _parse_aggregate_query(self, query: str) -> None:
        """解析aggregate查询中的字段"""
        try:
            # 提取aggregate()中的参数部分
            match = re.search(r'\.aggregate\s*\((.*)\)\s*;?\s*$', query, re.DOTALL | re.MULTILINE)
            if match:
                pipeline_str = match.group(1).strip()
                
                # 提取所有阶段
                stages = re.finditer(r'\{[^{}]*\}', pipeline_str)
                for stage_match in stages:
                    stage_str = stage_match.group(0)
                    stage = self._parse_mongodb_query(stage_str)
                    if isinstance(stage, dict):
                        if "fields" in stage:
                            for field in stage["fields"]:
                                self.fields.add(field)
                        
                        # 检查是否是$count阶段
                        count_match = re.search(r'"?\$count"?\s*:\s*"([^"]+)"', stage_str)
                        if count_match:
                            self.fields.add(count_match.group(1))
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")

    def _parse_find_query(self, query: str) -> None:
        """解析find查询中的字段"""
        try:
            # 提取find()中的参数部分
            match = re.search(r'\.find\s*\((.*)\)\s*;?\s*$', query, re.DOTALL | re.MULTILINE)
            if match:
                args_str = match.group(1).strip()
                
                # 提取所有参数
                args_matches = re.finditer(r'\{[^{}]*\}', args_str)
                args = []
                for arg_match in args_matches:
                    arg = self._parse_mongodb_query(arg_match.group(0))
                    if isinstance(arg, dict) and "fields" in arg:
                        args.append(arg["fields"])
                
                # 处理查询条件
                if len(args) > 0:
                    for field in args[0]:
                        self.fields.add(field)
                
                # 处理投影
                if len(args) > 1:
                    for field in args[1]:
                        self.fields.add(field)
        except Exception as e:
            print(f"Error parsing find query: {e}")

    def parse_query(self, query: str) -> list:
        """
        解析MongoDB查询语句，提取所有字段
        
        Args:
            query (str): MongoDB查询语句
            
        Returns:
            List[str]: 查询中使用的所有字段列表
        """
        self.fields.clear()
        
        try:
            # 清理查询字符串
            query = query.strip()
            
            if ".find(" in query:
                self._parse_find_query(query)
            elif ".aggregate(" in query:
                self._parse_aggregate_query(query)
                
            return sorted(list(self.fields))
        except Exception as e:
            print(f"Error parsing query: {e}")
            return []

def extract_fields(MQL: str) -> list:
    """
    从MongoDB查询语句中提取所有使用的字段
    
    Args:
        MQL (str): MongoDB查询语句
        
    Returns:
        List[str]: 查询中使用的所有字段列表
    """
    parser = MongoFieldParser()
    return parser.parse_query(MQL)

if __name__ == "__main__":

    test_queries = [
        "db.musical.aggregate([\n  {\n    $group: {\n      _id: \"$Result\",\n      count: { $sum: 1 }\n    }\n  },\n  {\n    $sort: { count: -1 }\n  },\n  {\n    $limit: 1\n  },\n  {\n    $project: {\n      _id: 0,\n      Result: \"$_id\"\n    }\n  }\n]);"
    ]

    parser = MongoFieldParser()
    for i, query in enumerate(test_queries, 1):
        print(f"\nTest Query {i}:")
        print(query)
        fields = parser.parse_query(query)
        print("Extracted fields:", fields)