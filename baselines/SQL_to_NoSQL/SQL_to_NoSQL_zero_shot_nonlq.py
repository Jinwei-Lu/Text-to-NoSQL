import json
import os
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply, get_SQL_Schemas

SYSTEM_PROMPT = "You are an expert in converting SQL queries to MongoDB queries. Please infer the corresponding MongoDB query based on the given SQL database schemas, SQL query, and MongoDB schema."

INSTRUCTION = """# Given a SQL database schemas, SQL query and MongoDB schema, perform the following actions:
1. Parse the intent of the SQL query based on SQL database schemas;
2. Establish mapping relationships between the SQL query and MongoDB Schema based on the query intent;
3. Infer the corresponding MongoDB query based on the SQL query intent and mapping relationships;
4. Output the MongoDB query without explanation in the following format:
```javascript
db.collection.aggregate([<pipeline>]); / db.collection.find({<filter>}, {<projection>});
```

NOTE: 
- The result documents in MongoDB queries can only contain fields from the MongoDB Schema or fields that follow the format `operation_MongoDBField`;
- The collection names after the `lookup` stage in MongoDB queries must be `Docs1`, `Docs2`, `Docs3`, etc., and cannot be other strings;"""

def prompt_maker(example:dict):
    db_id = example['db_id']
    sql_query = example['ref_sql']

    sql_schemas = get_SQL_Schemas(db_id=db_id)
    mongo_schemas = schemas_transform(db_id=db_id)
        
    prompt = """{}

## SQL Query: 
```sql
{}
```

## SQL Schema
```markdown
{}
```

## MongoDB Schema
```markdown
{}
```

A: Let's think step by step!
""".format(INSTRUCTION, sql_query, sql_schemas, mongo_schemas)

    return prompt

def generate_icl(example:dict):
    prompt = prompt_maker(example)

    messages = [
        {
            "role":"system",
            "content":SYSTEM_PROMPT
        },
        {
            "role":"user",
            "content":prompt
        }
    ]
    ans = generate_reply(messages=messages)[0]


    if "```javascript" in ans:
        ans = ans.rsplit("```javascript", 1)[1].split("```", 1)[0].strip()
    else:
        ans = "db." + ans.rsplit("db.", 1)[1].rsplit(";", 1)[0]
    return ans

if __name__ == "__main__":
    file_name = "./TEND/test_debug_rag20_deepseekv3.json"
    save_path = "./results/SQL_to_NoSQL/results_zero_shot_deepseekv3_nonlq.json"

    with open(file_name, "r") as f:
        test_data = json.load(f)

    if os.path.exists(save_path):
        with open(save_path, "r") as f:
            results = json.load(f)
    else:
        results = []

    for i, example in tqdm(enumerate(test_data), total=len(test_data)):
        if i < len(results):
            continue
        ans = generate_icl(example=example)
        
        example_new = {
            "record_id":example['record_id'],
            "db_id":example['db_id'],
            "NLQ":example['nlq'],
            "target":example['MQL'],
            "prediction":ans,
        }

        results.append(example_new)
    
        with open(save_path, "w") as f:
            json.dump(results, f, indent=4)