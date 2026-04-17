import json
import os
from example_prompt import ask1, ask2, ans1, ans2
from dataset_construct.utils  import execute_sql, generate_reply, get_query_from_reply, remove_ann, check, generate_claude_reply, generate_Yi_reply

schema_folder_path = "./mongodb_schema/"

def prompt_maker(schemas:dict, nlqs:list, ref_sql, db_name:str):
    schemas_sql = execute_sql(db_name, ref_sql)['schemas']
    instruction = "#### Generate the MongoDB query based on MongoDB Schemas and user requirements."
    schema_prompt = f"""### Schemas of all Collections in "{db_name}" Database\n"""
    nlqs_str = "\n# ".join(nlqs)
    ann = ""
    if len(nlqs) > 1:
        ann = "(All requirements correspond to the same MongoDB query)"
    for collection_name, schema in schemas.items():
        # print()
        schema_prompt += f"""# Collection: {collection_name}
{json.dumps(schema, indent=2)}

"""
    prompt = f"""{instruction}
```
{schema_prompt[:-2]}
```
### User Requirement{ann}: 
# {nlqs_str}
### The keys required to be displayed in the results: {schemas_sql}

### MongoDB Query
```javascript
"""
    return prompt
    

def generate(db_name:str, nlqs:list, ref_sql:str, model:str):
    schema_file_path = os.path.join(schema_folder_path, db_name + ".json")

    with open(schema_file_path, "r") as f:
        schemas = json.load(f)

    prompt = prompt_maker(schemas, nlqs, ref_sql, db_name)

    # print(prompt)
    if len(nlqs) > 1:
        ans = ans1
        ask = ask1
    else:
        ask = ask2
        ans = ans2

    if "gpt-4" in model:
        messages = [
            {
                "role":"system",
                "content":"You are an excellent MongoDB query writer, and you are very good at writing the MongoDB queries your users need by referring to SQL queries and database schemas."
            },
            {
                "role":"user",
                "content":prompt
            }
        ]
    else:

        messages = [
            {
                "role":"system",
                "content":"You are an excellent MongoDB query writer, and you are very good at writing the MongoDB queries your users need by referring to SQL queries and database schemas."
            },
            {
                "role":"user",
                "content":ask
            },
            {
                "role":"assistant",
                "content":ans
            },
            {
                "role":"user",
                "content":prompt
            }
        ]


    if "gpt" in model:
        reply = generate_reply(messages=messages, model=model)
    elif "claude" in model:
        reply = generate_claude_reply(messages=messages, model=model)
    elif "yi" in model:
        reply = generate_Yi_reply(messages=messages, model=model)
    else:
        raise TypeError("Don't support model type {}".format(model))

    # print(reply)

    query = get_query_from_reply(reply)

    return query

if __name__ == "__main__":
    ref_sql = "SELECT T1.customer_name , T1.customer_phone FROM customers AS T1 JOIN customer_orders AS T2 ON T1.customer_id = T2.customer_id JOIN order_items AS T3 ON T3.order_id = T2.order_id GROUP BY T1.customer_id ORDER BY sum(T3.order_quantity) DESC LIMIT 1"
    nlqs = [
            "What are the name and phone of the customer with the most ordered product quantity?"
        ]
    db_id = "customers_and_products_contacts"

    query = generate(db_id, nlqs, ref_sql=ref_sql, model="claude-3-opus-20240229")
    print(query)

    query = remove_ann(query=query)

    check_info = check(query=query, db_name=db_id, ref_sql=ref_sql)
    print(check_info)