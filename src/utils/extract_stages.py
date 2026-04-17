import re
from typing import List, Dict, Any

def _extract_regex_operators(query_str: str) -> bool:
    """检查查询字符串中是否包含正则表达式操作符"""
    # 检查显式的 $regex 操作符
    if '$regex' in query_str:
        return True
    # 检查 /pattern/i 格式的正则表达式
    if re.search(r'/[^/]+/[i]?', query_str):
        return True
    return False

def _extract_expr_operators(match_str: str) -> List[str]:
    """从$expr中提取操作符"""
    operators = []
    if '$expr' in match_str:
        operators.append('expr')
        # 提取比较操作符
        for op in ['$eq', '$gt', '$gte', '$lt', '$lte', '$ne', '$not']:
            if op in match_str:
                operators.append(op.lstrip('$'))
    return operators

def _parse_pipeline_stage(stage_str: str) -> List[str]:
    """解析单个管道阶段"""
    operators = []
    
    # 提取主要操作符
    stage_ops = re.findall(r'\$(\w+):', stage_str)
    if stage_ops:
        main_op = stage_ops[0]
        operators.append(main_op)
        
        # 处理match阶段
        if main_op == 'match':
            # 检查 $not 操作符
            if '$not' in stage_str:
                operators.append('not')
            # 只在没有 $not 操作符时检查正则表达式
            elif _extract_regex_operators(stage_str):
                operators.append('regex')
            # 检查表达式操作符
            operators.extend(_extract_expr_operators(stage_str))
            
        # 处理lookup阶段中的子管道
        elif main_op == 'lookup' and 'pipeline:' in stage_str:
            # 提取子管道
            pipeline_match = re.search(r'pipeline:\s*\[(.*?)\]', stage_str, re.DOTALL)
            if pipeline_match:
                sub_pipeline = pipeline_match.group(1)
                # 解析子管道中的每个阶段
                sub_stages = re.findall(r'\{(.*?)\}', sub_pipeline, re.DOTALL)
                for sub_stage in sub_stages:
                    operators.extend(_parse_pipeline_stage(sub_stage))
    
    return operators

def get_query_stages(query: str) -> List[str]:
    """
    从MongoDB查询语句中提取所有的操作阶段
    
    Args:
        query (str): MongoDB查询语句
        
    Returns:
        List[str]: 查询中的所有操作阶段列表
    """
    stages = []
    
    # 清理查询字符串
    query = query.strip()
    
    if ".find(" in query:
        # 处理find查询
        try:
            # 提取find参数
            find_match = re.search(r'\.find\s*\((.*?)\)(?:\s*\.|\s*;|\s*$)', query, re.DOTALL)
            if find_match:
                find_args = find_match.group(1)
                
                # 分割查询条件和投影
                args = re.findall(r'\{(.*?)\}', find_args, re.DOTALL)
                
                # 处理查询条件
                if args and args[0].strip():
                    stages.append("match")
                    # 检查正则表达式
                    if _extract_regex_operators(args[0]):
                        stages.append("regex")
                    # 检查 $not 操作符
                    if '$not' in args[0]:
                        stages.append("not")
                
                # 处理投影
                if len(args) > 1 and args[1].strip():
                    stages.append("project")
                
                # 检查排序
                if ".sort(" in query:
                    stages.append("sort")
                    
                # 检查限制
                if ".limit(" in query:
                    stages.append("limit")
                    
        except Exception as e:
            print(f"Error parsing find query: {e}")
            
    elif ".aggregate(" in query:
        # 处理aggregate查询
        try:
            # 提取aggregate管道
            agg_match = re.search(r'\.aggregate\s*\((.*?)\)(?:\s*;|\s*$)', query, re.DOTALL)
            if agg_match:
                pipeline_str = agg_match.group(1)
                
                # 提取每个管道阶段
                stages_matches = re.findall(r'\{(.*?)\}', pipeline_str, re.DOTALL)
                
                # 解析每个阶段
                for stage_str in stages_matches:
                    stages.extend(_parse_pipeline_stage(stage_str))
                    
        except Exception as e:
            print(f"Error parsing aggregate query: {e}")
    
    return stages

if __name__ == "__main__":
    query_string1 = """db.cinema.find(
        { "year": { "$gte": 2000 } },
        { "Name": 1, "Openning_year": 1, "Capacity": 1, "_id": 0 }
    ).sort({"year": -1}).limit(10);"""

    query_string2 = """db.school.aggregate([
      {
        $lookup: {
          from: "driver",
          localField: "School_ID",
          foreignField: "school_bus.School_ID",
          as: "Docs1"
        }
      },
      {
        $unwind: "$Docs1"
      },
      {
        $project: {
          "School": 1,
          "Name": "$Docs1.Name",
          "_id": 0
        }
      }
    ]);"""
    
    query_string3 = """db.departments.aggregate([
    {
        $unwind: "$employees"
    },
    {
        $lookup: {
        from: "regions",
        let: { location_id: "$LOCATION_ID" },
        pipeline: [
            { $unwind: "$countries" },
            { $unwind: "$countries.locations" },
            {
            $match: {
                $expr: {
                $eq: ["$countries.locations.LOCATION_ID", "$$location_id"]
                }
            }
            },
            {
            $project: {
                COUNTRY_NAME: "$countries.COUNTRY_NAME"
            }
            }
        ],
        as: "Docs1"
        }
    },
    {
        $unwind: "$Docs1"
    },
    {
        $project: {
        FIRST_NAME: "$employees.FIRST_NAME",
        LAST_NAME: "$employees.LAST_NAME",
        EMPLOYEE_ID: "$employees.EMPLOYEE_ID",
        COUNTRY_NAME: "$Docs1.COUNTRY_NAME",
        _id: 0
        }
    }
    ]);"""

    query_string4 = """db.Staff.find(
      {
        email_address: { $regex: "wrau", $options: "i" }
      },
      {
        last_name: 1,
        _id: 0
      }
    );"""

    query_string5 = """db.jobs.aggregate([\n  {\n    $unwind: \"$employees\"\n  },\n  {\n    $match: {\n      \"employees.FIRST_NAME\": { $not: /M/i }\n    }\n  },\n  {\n    $project: {\n      EMPLOYEE_ID: \"$employees.EMPLOYEE_ID\",\n      FIRST_NAME: \"$employees.FIRST_NAME\",\n      LAST_NAME: \"$employees.LAST_NAME\",\n      HIRE_DATE: \"$employees.HIRE_DATE\",\n      SALARY: \"$employees.SALARY\",\n      DEPARTMENT_ID: \"$employees.DEPARTMENT_ID\",\n      _id: 0\n    }\n  }\n]);"""

    print("\n" + "="*80 + "\nTest Case 1:")
    stages1 = get_query_stages(query_string1)
    print("Query:\n", query_string1)
    print("Extracted Stages: ", stages1)
    print("Target Stages: ", '["match", "project", "sort", "limit"]')

    print("\n" + "="*80 + "\nTest Case 2:")
    stages2 = get_query_stages(query_string2)
    print("Query:\n", query_string2)
    print("Extracted Stages: ", stages2)
    print("Target Stages: ", '["lookup", "unwind", "project"]')

    print("\n" + "="*80 + "\nTest Case 3:")
    stages3 = get_query_stages(query_string3)
    print("Query:\n", query_string3)
    print("Extracted Stages: ", stages3)
    print("Target Stages: ", '["unwind", "lookup", "unwind", "unwind", "match", "expr", "eq", "project", "unwind", "project"]')

    print("\n" + "="*80 + "\nTest Case 4:")
    stages4 = get_query_stages(query_string4)
    print("Query:\n", query_string4)
    print("Extracted Stages: ", stages4)
    print("Target Stages: ", '["match", "regex", "project"]')

    print("\n" + "="*80 + "\nTest Case 5:")
    stages5 = get_query_stages(query_string5)
    print("Query:\n", query_string5)
    print("Extracted Stages: ", stages5)
    print("Target Stages: ", '["unwind", "match", "not", "project"]')