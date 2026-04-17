#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
from sql_to_mongo import SQLToMongoConverter

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_conversion(converter, sql, case_name=""):
    """测试SQL转换并打印结果"""
    print(f"\n测试用例: {case_name}")
    print(f"SQL查询: {sql}")
    print("-" * 50)
    try:
        result = converter.convert_sql(sql)
        print("MongoDB查询:")
        print(result["mongo_query"])
        return True
    except Exception as e:
        print(f"转换失败: {e}")
        return False
    finally:
        print("-" * 50)

def main():
    # 创建转换器实例
    try:
        converter = SQLToMongoConverter()
    except Exception as e:
        logger.error(f"初始化转换器失败: {e}")
        return

    # 测试用例
    test_cases = [
        {
            "name": "基本查询",
            "sql": "SELECT * FROM users WHERE age > 18"
        },
        {
            "name": "带排序的查询",
            "sql": "SELECT name, age FROM users WHERE city = 'Beijing' ORDER BY age DESC"
        },
        {
            "name": "分组查询",
            "sql": "SELECT dept, COUNT(*) FROM employees GROUP BY dept HAVING COUNT(*) > 5"
        },
        {
            "name": "日期范围查询",
            "sql": "SELECT * FROM orders WHERE order_date BETWEEN '2023-01-01' AND '2023-12-31'"
        },
        {
            "name": "复杂条件查询",
            "sql": "SELECT * FROM products WHERE price > 100 AND (category = 'Electronics' OR category = 'Books')"
        },
        {
            "name": "IN查询",
            "sql": "SELECT * FROM customers WHERE country IN ('USA', 'Canada', 'Mexico')"
        },
        {
            "name": "模糊匹配查询",
            "sql": "SELECT * FROM products WHERE name LIKE '%phone%'"
        },
        {
            "name": "聚合函数查询",
            "sql": "SELECT category, AVG(price) as avg_price FROM products GROUP BY category"
        },
        {
            "name": "JOIN查询",
            "sql": "SELECT o.order_id, c.name FROM orders o INNER JOIN customers c ON o.customer_id = c.id"
        },
        {
            "name": "高于平均价格的产品（使用聚合）",
            "sql": "SELECT category, price FROM products WHERE price > (SELECT AVG(price) FROM products)"
        }
    ]
    
    # 执行测试
    success_count = 0
    total_count = len(test_cases)
    
    for case in test_cases:
        if test_conversion(converter, case["sql"], case["name"]):
            success_count += 1
    
    # 打印统计信息
    print("\n测试统计:")
    print(f"总用例数: {total_count}")
    print(f"成功用例数: {success_count}")
    print(f"失败用例数: {total_count - success_count}")
    print(f"成功率: {(success_count/total_count)*100:.2f}%")

if __name__ == "__main__":
    main() 