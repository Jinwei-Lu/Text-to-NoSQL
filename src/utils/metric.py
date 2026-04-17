import demjson
import json
from dataclasses import dataclass
from pathlib import Path
from pymongo import MongoClient
from tqdm import tqdm
from typing import List, Dict, Tuple
import re
import time
from contextlib import contextmanager

from extract_fields import extract_fields
from extract_stages import get_query_stages
from mongosh_exec import MongoShellExecutor

@dataclass
class MetricConfig:
    """评估指标的配置类"""
    mongodb_uri: str = 'mongodb://localhost:27017/'
    wrong_examples_path: Path = Path('./wrong_examples_icl.json')
    metrics_list: List[str] = ('EX', 'EM', 'QSM', 'QFC', 'EFM', 'EVM')
    
    # 简化配置
    cache_size: int = 1000  # 缓存大小
    timeout: int = 30       # 查询超时时间(秒)

class QueryComparator:
    """查询比较器"""
    
    def __init__(self, config: MetricConfig):
        self.client = MongoClient(config.mongodb_uri)
        self.executor = MongoShellExecutor()
        
    def _get_query_result(self, db_id: str, query: str) -> List[Dict]:
        """执行查询并返回结果"""
        result = self.executor.execute_query(db_id, query)
        # 清理JSON结果中的多余引号
        if isinstance(result, str):
            result = result.replace('"""', '"')
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                # 如果还是无法解析，尝试使用demjson
                try:
                    result = demjson.decode(result)
                except:
                    print(f"Warning: Unable to parse result for query: {query}")
                    result = []
        return result
    
    def _deal_query(self, query: str) -> str:
        """处理查询字符串，去除回车等字符
        
        这个方法用于清理和标准化查询字符串:
        1. 使用strip()去除字符串两端的空白字符
        2. 使用正则表达式将查询中的连续空白字符(包括空格、制表符、换行符等)替换为单个空格
        
        参数:
            query: 输入的查询字符串
            
        返回:
            处理后的标准化查询字符串
        """
        query = re.sub(r'\s+', ' ', query.strip())
        return query

    def _compare_values(self, val1, val2) -> bool:
        """递归比较两个值是否相等，支持嵌套结构"""
        if isinstance(val1, dict) and isinstance(val2, dict):
            if set(val1.keys()) != set(val2.keys()):
                return False
            return all(self._compare_values(val1[k], val2[k]) for k in val1)
        elif isinstance(val1, (list, tuple)) and isinstance(val2, (list, tuple)):
            if len(val1) != len(val2):
                return False
            return all(self._compare_values(v1, v2) for v1, v2 in zip(val1, val2))
        else:
            return val1 == val2

    def compare(self, query1: str, query2: str, db_id: str) -> Dict[str, int]:
        """比较两个查询的执行结果和结构"""
        metrics = {metric: 0 for metric in MetricConfig.metrics_list}
        
        # 处理查询字符串，计算EM
        parsed_query1 = self._deal_query(query1)
        parsed_query2 = self._deal_query(query2)
        metrics['EM'] = parsed_query1 == parsed_query2
        
        # 提取和比较查询阶段，计算QSM
        try:
            stages1 = get_query_stages(query=query1)
            stages2 = get_query_stages(query=query2)
            metrics['QSM'] = stages1 == stages2
        except Exception as e:
            print(f"Error calculating QSM for db_id {db_id}: {str(e)}")
            metrics['QSM'] = 0
            
        # 提取和比较字段，计算QFC
        try:
            fields1 = extract_fields(MQL=query1, db_name=db_id)
            fields2 = extract_fields(MQL=query2, db_name=db_id)
            metrics['QFC'] = set(fields1) == set(fields2)
        except Exception as e:
            print(f"Error calculating QFC for db_id {db_id}: {str(e)}")
            metrics['QFC'] = 0
            
        # 执行查询并比较结果，计算EX, EFM, EVM
        try:
            result1 = self._get_query_result(db_id, query1)
            result2 = self._get_query_result(db_id, query2)
            
            # 使用递归比较函数比较整个结果
            metrics['EX'] = self._compare_values(result1, result2)
            
            # 只有在成功获取结果后才计算EFM和EVM
            target_fields1, target_fields2 = set(), set()
            metrics['EFM'] = 1
            metrics['EVM'] = 1
            
            def get_all_fields(d, fields):
                """递归获取所有字段"""
                if isinstance(d, dict):
                    for k, v in d.items():
                        fields.add(k)
                        get_all_fields(v, fields)
                elif isinstance(d, (list, tuple)):
                    for item in d:
                        get_all_fields(item, fields)

            # 递归获取所有字段
            for res1, res2 in zip(result1, result2):
                get_all_fields(res1, target_fields1)
                get_all_fields(res2, target_fields2)
                if not self._compare_values(res1, res2):
                    metrics['EVM'] = 0
                    
            if target_fields1 != target_fields2:
                metrics['EFM'] = 0
                
        except Exception as e:
            print(f"Error executing queries for db_id {db_id}: {str(e)}")
            metrics['EX'] = 0
            metrics['EFM'] = 0
            metrics['EVM'] = 0
            
        return metrics

@contextmanager
def timer(name: str):
    start = time.time()
    yield
    print(f"{name} took {time.time() - start:.2f} seconds")

class AccuracyCalculator:
    """准确率计算器"""
    
    def __init__(self, config: MetricConfig):
        self.config = config
        self.comparator = QueryComparator(config)
        
    def calculate(self, examples: List[Dict], need_print: bool = False, need_save: bool = False) -> Tuple[Dict[str, float], str]:
        """计算准确率"""
        metrics = {metric: 0 for metric in self.config.metrics_list}
        wrong_examples = []
        total_examples = len(examples)
        
        with timer("Processing examples"):
            for example in tqdm(examples, desc="Processing examples"):
                try:
                    example_acc = self.comparator.compare(
                        example['target'],
                        example['prediction'],
                        example['db_id']
                    )
                    
                    for metric, acc in example_acc.items():
                        # 确保累加的值是0或1
                        metrics[metric] += min(1, max(0, acc))
                        
                    if example_acc.get('EX', 1) == 0:
                        formatted_example = self._format_example(example, example_acc)
                        wrong_examples.append(formatted_example)
                        
                except Exception as e:
                    print(f"\nError processing example:")
                    print(f"DB ID: {example['db_id']}")
                    print(f"Error: {str(e)}\n")

        # 使用总样本数计算平均值
        if total_examples > 0:
            for metric in metrics:
                metrics[metric] = metrics[metric] / total_examples
                # 确保最终结果不超过1（100%）
                metrics[metric] = min(1.0, metrics[metric])
                
        acc_str = self._format_metrics_string(metrics)
        
        if need_print:
            print(acc_str)
            if wrong_examples:
                print(f"\nTotal errors: {len(wrong_examples)} out of {len(examples)} examples")
        if need_save:
            self._save_wrong_examples(wrong_examples)
            
        return metrics, acc_str
    
    def _format_example(self, example: Dict, acc: Dict) -> Dict:
        """格式化示例数据"""
        return {
            "NLQ": example['NLQ'],
            "db_id": example['db_id'],
            "prediction": example['prediction'],
            "target": example['target'],
            "flag": acc['EX'] == 1
        }
    
    def _format_metrics_string(self, metrics: Dict[str, float]) -> str:
        """格式化指标字符串"""
        return f"""Exact Match: {metrics['EM']}
    Query Stages Match(QSM): {metrics['QSM']}
    Query Fields Coverage(QFC): {metrics['QFC']}
Execution Accuracy: {metrics['EX']}
    Execution Fields Match(EFM): {metrics['EFM']}
    Execution Value Match(EVM): {metrics['EVM']}"""
    
    def _save_wrong_examples(self, wrong_examples: List[Dict]):
        """保存错误示例"""
        with open(self.config.wrong_examples_path, "w") as f:
            json.dump(wrong_examples, f, indent=4)

#  python ./src/utils/metric.py

if __name__ == "__main__":
    # 文件名
    file_name = "no_pref/test_debug_rag_exec20_deepseekv3_ori_no_pref"
    # file_name = "test_debug_rag_exec20_deepseekv3_ori"
    print(f"文件名: {file_name}")

    # 数据路径
    # predictions_path = f"./TEND/{file_name}.json"
    predictions_path = f"./results/{file_name}.json"

    # 配置
    config = MetricConfig(
        cache_size=10000,  # 增加缓存大小
        wrong_examples_path=Path(f'./error_case/{file_name}.json'),
    )
    calculator = AccuracyCalculator(config)
    
    # 加载数据
    with open(predictions_path, 'r', encoding='utf-8') as f:
        predictions = json.load(f)
    
    # 重构数据格式
    results = [{
        "db_id": example['db_id'],
        "NLQ": example['nlq'],
        "target": example['MQL'],
        "prediction": example['MQL_debug_exec'],
    } for example in predictions]

    # 计算指标
    metric, metric_str = calculator.calculate(results, need_print=True)