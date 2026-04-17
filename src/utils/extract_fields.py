from typing import List, Dict
import json
import os

class MongoFieldParser:
    def __init__(self, db_name: str):
        """初始化解析器"""

        # 获取schema文件路径
        schema_file = "./mongodb_schema/" + db_name + ".json"

        with open(schema_file, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        
        # 提取所有字段
        self.all_fields = self._extract_schema_fields(schema) if schema else set()
    
    def _extract_schema_fields(self, schema: Dict, prefix: str = "") -> set:
        """从schema中提取所有字段名（不包含路径）"""
        fields = set()
        
        def process_object(obj: Dict):
            for key, value in obj.items():
                # 添加当前字段名
                fields.add(key)
                
                if isinstance(value, dict):  # 处理嵌套对象
                    process_object(value)
                elif isinstance(value, list) and value:  # 处理数组
                    if isinstance(value[0], dict):
                        process_object(value[0])
        
        process_object(schema)
        return fields
    
    def parse_query(self, query: str) -> List[str]:
        """解析MongoDB查询语句,提取所有使用的字段"""
        found_fields = set()
        
        # 预处理查询字符串 - 移除空白字符
        query = ' '.join(query.split())
        
        # 遍历所有字段，检查是否在查询中使用
        for field in self.all_fields:
            # 检查常见的字段引用方式
            if (f'"{field}"' in query or          # "HIRE_DATE"
                f"'{field}'" in query or          # 'HIRE_DATE'
                f"${field}" in query or           # $HIRE_DATE
                f"{field}:" in query or           # HIRE_DATE:
                f".${field}" in query):           # .$HIRE_DATE
                found_fields.add(field)
        
        return sorted(list(found_fields))

def extract_fields(MQL: str, db_name: str) -> List[str]:
    """从MongoDB查询语句中提取所有使用的字段"""
    parser = MongoFieldParser(db_name=db_name)
    return parser.parse_query(MQL)

if __name__ == "__main__":  
    # 测试查询
    query = "db.jobs.aggregate([\n  {\n    $unwind: \"$employees\"\n  },\n  {\n    $match: {\n      \"employees.FIRST_NAME\": {\n        $not: {\n          $regex: \"M\",\n          $options: \"i\"\n        }\n      }\n    }\n  },\n  {\n    $project: {\n      FIRST_NAME: \"$employees.FIRST_NAME\",\n      LAST_NAME: \"$employees.LAST_NAME\",\n      HIRE_DATE: \"$employees.HIRE_DATE\",\n      SALARY: \"$employees.SALARY\",\n      DEPARTMENT_ID: \"$employees.DEPARTMENT_ID\",\n      _id: 0\n    }\n  }\n]);\n"
    

    # 打印查询
    print(f"Query: {query}")
    fields = extract_fields(query, db_name="hr_1")
    print(f"Found fields: {json.dumps(fields, indent=4)}")
