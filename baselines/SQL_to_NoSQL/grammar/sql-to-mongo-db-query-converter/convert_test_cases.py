#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import sys
from pathlib import Path
from tqdm import tqdm
from sql_to_mongo import SQLToMongoConverter

def load_test_cases(file_path):
    """加载测试用例"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"找不到测试文件: {file_path}")
    
    with open(file_path, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            raise

def save_results(results, output_dir):
    """保存结果到指定目录"""
    try:
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, 'sql_to_nosql_results.json')
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    except Exception:
        raise

def process_test_cases(test_cases, converter):
    """处理测试用例"""
    results = []
    error_types = {}
    
    # 使用tqdm创建进度条
    pbar = tqdm(test_cases, desc="转换进度", unit="条")
    
    for case in pbar:
        try:
            # 获取必要信息
            record_id = case['record_id']
            db_id = case['db_id']
            sql = case['ref_sql']
            original_mql = case['MQL']
            
            # 更新进度条描述
            pbar.set_description(f"处理 {db_id}")
            
            # 转换SQL到MongoDB查询
            conversion_result = converter.convert_sql(sql)
            converted_mql = conversion_result['mongo_query']
            
            # 构建结果
            result = {
                'record_id': record_id,
                'db_id': db_id,
                'sql': sql,
                'original_mql': original_mql,
                'converted_mql': converted_mql,
                'success': True
            }
            
        except Exception as e:
            error_msg = str(e)
            error_type = error_msg.split(':')[0] if ':' in error_msg else error_msg
            error_types[error_type] = error_types.get(error_type, 0) + 1
            
            result = {
                'record_id': case.get('record_id'),
                'db_id': case.get('db_id'),
                'sql': case.get('ref_sql'),
                'original_mql': case.get('MQL'),
                'converted_mql': None,
                'success': False,
                'error': error_msg
            }
        
        results.append(result)
    
    # 在进度条完成后显示统计信息
    success_count = sum(1 for r in results if r['success'])
    total_count = len(results)
    pbar.set_postfix({
        "成功": f"{success_count}/{total_count}",
        "成功率": f"{(success_count/total_count)*100:.1f}%"
    })
    
    return results, error_types

def main():
    try:
        # 获取当前文件的绝对路径
        current_file = Path(__file__).resolve()
        
        # 获取项目根目录
        project_root = current_file.parents[4]
        
        # 构建输入和输出路径
        test_file = project_root / 'TEND' / 'test.json'
        output_dir = project_root / 'results' / 'SQL_to_NoSQL'
        
        # 创建转换器实例
        converter = SQLToMongoConverter()
        
        # 加载并处理测试用例
        test_cases = load_test_cases(test_file)
        results, _ = process_test_cases(test_cases, converter)
        
        # 保存结果
        save_results(results, output_dir)
        
    except Exception as e:
        sys.exit(1)

if __name__ == "__main__":
    main() 