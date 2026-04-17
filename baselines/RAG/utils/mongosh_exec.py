import subprocess
import json
import os
from typing import Union, List, Dict
from datetime import datetime
from tqdm import tqdm

class MongoShellExecutor:
    def __init__(self, connection_string: str = "mongodb://localhost:27017", output_dir: str = "output"):
        self.connection_string = connection_string
        self.mongosh_path = self._get_mongosh_path()
        self.output_dir = output_dir
        
        # 创建输出目录
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        
        # 测试连接
        self._test_connection()

    def _get_mongosh_path(self) -> str:
        """获取mongosh可执行文件的路径"""
        # 检查多个可能的路径
        possible_paths = [
            r"C:\Program Files\MongoDB\Server\7.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\6.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\5.0\bin\mongosh.exe",
            r"C:\Program Files\MongoDB\Server\4.4\bin\mongo.exe",
            r"C:\Program Files\MongoDB\Server\4.2\bin\mongo.exe",
            r"D:\MongoDB\Server\7.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\6.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\5.0\bin\mongosh.exe",
            r"D:\MongoDB\Server\4.4\bin\mongo.exe",
            r"D:\MongoDB\Server\4.2\bin\mongo.exe"
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                print(f"找到MongoDB执行文件: {path}")
                return path
            
        raise FileNotFoundError("找不到mongo或mongosh。请确保已安装MongoDB并将其添加到系统路径中。")

    def _format_query(self, query: str) -> str:
        """格式化查询字符串"""
        query = query.strip().rstrip(';')
        
        if ('.find(' in query or '.aggregate(' in query) and '.toArray()' not in query:
            query += '.toArray()'
            
        return query

    def _save_to_json(self, data: Union[List, Dict], filename: str = None) -> str:
        """保存结果到JSON文件"""
        if filename is None:
            # 使用时间戳生成文件名
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"query_result_{timestamp}.json"
        
        filepath = os.path.join(self.output_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        return filepath

    def execute_query(self, db_name: str, query: str, output_file: str = None, timeout: int = 30, get_str: bool = False) -> List[Dict]:
        try:
            formatted_query = self._format_query(query)
            js_command = f'''
                try {{
                    db = connect("{self.connection_string}").getSiblingDB("{db_name}");
                    result = {formatted_query};
                    printjson(result);
                }} catch (e) {{
                    print("QUERY_ERROR: " + e.message);
                    quit(1);
                }}
            '''
            
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                [self.mongosh_path, "--quiet", "--eval", js_command],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            
            if result.stderr:
                print(f"查询错误: {result.stderr}")
                return []
            
            output = result.stdout.strip()
            if not output:
                print("查询结果为空")
                return []
            
            if "QUERY_ERROR" in output:
                print(f"查询执行错误: {output}")
                return []
            
            try:
                # 处理MongoDB特殊类型
                special_types = [
                    ('ObjectId\\(([^)]+)\\)', r'"ObjectId(\1)"'),
                    ('ISODate\\(([^)]+)\\)', r'"ISODate(\1)"'),
                    ('NumberLong\\(([^)]+)\\)', r'\1'),
                    ('NumberDecimal\\(([^)]+)\\)', r'\1'),
                    ('Timestamp\\(([^)]+)\\)', r'"Timestamp(\1)"'),
                    ('BinData\\(([^)]+)\\)', r'"BinData(\1)"'),
                    ('DBRef\\(([^)]+)\\)', r'"DBRef(\1)"'),
                    ('NumberInt\\(([^)]+)\\)', r'\1'),
                    ('Date\\(([^)]+)\\)', r'"Date(\1)"')
                ]
                
                import re
                for pattern, replacement in special_types:
                    output = re.sub(pattern, replacement, output)
                    
                data = json.loads(output)
                if not isinstance(data, list):
                    data = [data]
                    
                # 验证转换后的数据
                for item in data:
                    if not isinstance(item, (dict, list)):
                        print(f"警告：数据项类型异常 - {type(item)}")
                        
                return data
                
            except json.JSONDecodeError as e:
                if get_str:
                    out_str = f"Error in Executing Query and Transfroming result into JSON: `{str(e)}`"
                    return out_str
                else:
                    print(f"JSON 解析错误: {str(e)}")
                    print(f"错误位置: {e.pos}")
                    print(f"错误行: {e.lineno}, 列: {e.colno}")
                print(f"原始输出片段: {output[max(0, e.pos-50):e.pos+50]}")
                return []
                
            except Exception as e:
                if get_str:
                    out_str = f"Error in Executing Query and Transfroming result into JSON: `{str(e)}`"
                    return out_str
                else:
                    print(f"数据转换错误: {str(e)}")
                    print(f"原始输出: {output[:200]}...")  # 只显示前200个字符
                return []
                
        except subprocess.TimeoutExpired:
            if get_str:
                out_str = f"Query Timeout: `{timeout} seconds`"
                return out_str
            else:
                print(f"查询超时 (>{timeout}秒)")
            return []
        except Exception as e:
            if get_str:
                out_str = f"Error in Executing Query and Transfroming result into JSON: `{str(e)}`"
                return out_str
            else:
                print(f"执行查询时发生错误: {str(e)}")
            return []

    def execute_script(self, db_name: str, script_path: str) -> str:
        """执行MongoDB脚本文件"""
        try:
            command = [self.mongosh_path, db_name, script_path]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            print(f"执行脚本时出错: {e.stderr}")
            raise

    def _test_connection(self):
        """测试数据库连接"""
        try:
            print(f"正在尝试连接到 MongoDB: {self.connection_string}")
            js_command = f'''
                try {{
                    db = connect("{self.connection_string}");
                    print("CONNECTION_SUCCESS");
                }} catch (e) {{
                    print("CONNECTION_ERROR: " + e.message);
                    quit(1);
                }}
            '''
            
            # 使用 UTF-8 编码运行命令
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(
                [self.mongosh_path, "--quiet", "--eval", js_command],
                capture_output=True,
                text=True,
                timeout=5,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo
            )
            
            if result.stderr:
                print(f"连接错误输出: {result.stderr}")
                raise Exception(f"MongoDB连接错误: {result.stderr}")
                
            if result.stdout:
                print(f"连接输出: {result.stdout}")
            
            if "CONNECTION_SUCCESS" not in result.stdout:
                raise Exception("MongoDB连接失败")
                
            print("MongoDB连接测试成功！")
            
        except Exception as e:
            print(f"MongoDB连接测试失败: {str(e)}")
            print("\n请确保：")
            print("1. MongoDB 服务已经启动（net start MongoDB）")
            print("2. 端口 27017 未被占用")
            print("3. 防火墙未阻止连接")
            print(f"4. MongoDB路径正确: {self.mongosh_path}")
            raise

def main():
    try:
        executor = MongoShellExecutor(
            connection_string="mongodb://localhost:27017/?connectTimeoutMS=5000",
            output_dir="query_results"
        )
        
        # 读取测试数据
        test_file = "./TEND/test_debug_rag_exec20_deepseekv3_ori.json"
        print(f"\n正在读取测试文件: {test_file}")
        
        with open(test_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        print(f"成功加载测试数据，共 {len(data)} 条记录")
        
        for i, example in tqdm(enumerate(data, 1), total=len(data)):
            try:
                # print(f"\n执行查询 {i}/{len(data)}:")
                # print(f"数据库: {example['db_id']}")
                # print(f"查询: {example['MQL']}")
                
                result = executor.execute_query(
                    example["db_id"], 
                    example['MQL_debug_exec'],
                    get_str=True
                )
                
                if isinstance(result, str):
                    print(f"查询结果: {result}")
                # else:
                #     print(f"查询结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
                
            except Exception as e:
                print(f"处理记录 {example.get('record_id', i)} 时出错: {str(e)}")
                continue
            
    except Exception as e:
        print(f"程序执行错误: {str(e)}")

if __name__ == "__main__":
    main()
