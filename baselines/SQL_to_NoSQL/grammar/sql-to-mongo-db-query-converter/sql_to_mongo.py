#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import subprocess
import json
import logging
from typing import Optional, Dict, Union, Any

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SQLToMongoConverter:
    def __init__(self, jar_path: Optional[str] = None):
        """
        初始化转换器
        :param jar_path: jar包路径，如果为None则在当前目录查找
        """
        self.jar_path = jar_path or self._find_jar()
        if not self.jar_path:
            raise FileNotFoundError("找不到sql-to-mongo-db-query-converter的jar包")
        
        # 验证jar包是否存在且可访问
        if not os.path.isfile(self.jar_path):
            raise FileNotFoundError(f"找不到jar包: {self.jar_path}")
        
        # 验证Java环境
        try:
            result = subprocess.run(["java", "-version"], capture_output=True, text=True)
            logger.info(f"Java版本信息: {result.stderr}")  # java -version 输出到stderr
        except Exception as e:
            raise RuntimeError(f"Java环境检查失败: {e}")
        
        logger.info(f"使用jar包: {os.path.abspath(self.jar_path)}")

    def _find_jar(self) -> Optional[str]:
        """
        在当前目录及target目录下查找jar包
        :return: jar包路径或None
        """
        # 可能的jar包名称模式
        jar_patterns = [
            "sql-to-mongo-db-query-converter-*-standalone.jar",
            "target/sql-to-mongo-db-query-converter-*-standalone.jar"
        ]
        
        for pattern in jar_patterns:
            try:
                result = subprocess.run(
                    f"find . -name '{pattern}'",
                    shell=True,
                    capture_output=True,
                    text=True
                )
                if result.stdout.strip():
                    found_jar = result.stdout.strip().split('\n')[0]
                    logger.debug(f"找到jar包: {found_jar}")
                    return found_jar
            except Exception as e:
                logger.warning(f"查找jar包时出错: {e}")
        
        return None

    def convert_sql(self, sql: str) -> Dict[str, Any]:
        """
        转换SQL查询到MongoDB查询
        :param sql: SQL查询语句
        :return: 包含MongoDB查询信息的字典
        """
        try:
            # 记录输入的SQL
            logger.info(f"输入SQL: {sql}")
            
            # 构建命令
            cmd = ["java", "-jar", self.jar_path, "-sql", sql]
            logger.debug(f"执行命令: {' '.join(cmd)}")
            
            # 执行命令
            result = subprocess.run(
                cmd, 
                capture_output=True, 
                text=True,
                encoding='utf-8'
            )
            
            # 检查返回码
            if result.returncode != 0:
                error_msg = f"转换失败 (返回码: {result.returncode})\n错误输出: {result.stderr}"
                logger.error(error_msg)
                raise Exception(error_msg)
            
            # 记录原始输出
            logger.debug(f"原始输出:\n{result.stdout}")
            
            # 提取MongoDB查询
            mongo_query = self._parse_output(result.stdout)
            logger.info(f"转换成功: {mongo_query['mongo_query']}")
            
            return mongo_query
            
        except Exception as e:
            logger.error(f"转换过程中出错: {e}")
            raise

    def _parse_output(self, output: str) -> Dict[str, Any]:
        """
        解析Java程序的输出，提取MongoDB查询
        :param output: Java程序的输出
        :return: 解析后的MongoDB查询
        """
        try:
            # 查找MongoDB查询部分
            start_idx = output.find("db.")
            if start_idx == -1:
                # 尝试查找其他可能的输出格式
                if "******Result:*********" in output:
                    # 提取Result部分
                    result_start = output.find("******Result:*********") + len("******Result:*********")
                    query_str = output[result_start:].strip()
                    logger.debug(f"从Result部分提取查询: {query_str}")
                else:
                    raise ValueError("输出中没有找到MongoDB查询")
            else:
                query_str = output[start_idx:].strip()
                logger.debug(f"提取到的查询字符串: {query_str}")
            
            # 构造返回结果
            result = {
                "mongo_query": query_str,
                "raw_output": output
            }
            
            return result
            
        except Exception as e:
            logger.error(f"解析输出时出错: {e}")
            raise

def main():
    """
    主函数，用于命令行调用
    """
    import argparse
    
    parser = argparse.ArgumentParser(description='SQL到MongoDB查询转换器')
    parser.add_argument('--sql', required=True, help='SQL查询语句')
    parser.add_argument('--jar', help='jar包路径（可选）')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    
    args = parser.parse_args()
    
    # 设置日志级别
    if args.debug:
        logger.setLevel(logging.DEBUG)
    
    try:
        converter = SQLToMongoConverter(args.jar)
        result = converter.convert_sql(args.sql)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        logger.error(f"执行失败: {e}")
        exit(1)

if __name__ == "__main__":
    main() 