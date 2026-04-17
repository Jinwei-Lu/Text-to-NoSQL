import openai
import numpy as np
from scipy.spatial.distance import cosine
import json
import pandas as pd
from tqdm import tqdm
import pickle
import time
import os


api_base = "https://openkey.cloud/v1"

# 替换为你的OpenAI API密钥
api_key = "sk-u4QlUZW8nLtaiqyv40338f916f5a4d6c81B42aB83b6fE567"

client = openai.Client(api_key=api_key, base_url=api_base)
cache = {}

def get_embedding(text, model="text-embedding-ada-002"):
    if text in cache:
        return cache[text]
    embedding = None
    while embedding is None:
        try:
            embedding = client.embeddings.create(input=text, model=model)
        except Exception as ex:
            print(ex)
            
    cache[text] = np.array(embedding.data[0].embedding)
    return np.array(embedding.data[0].embedding)


if __name__ == '__main__':
    data_path = "./TEND/test_SLM_prediction.json"
    vql_embedding_file_path = "./vector_store/test.pkl"

    data_new = []
    # if os.path.exists(vql_embedding_file_path):
    #     with open(vql_embedding_file_path, 'rb') as f: 
    #         data_new = pickle.load(f)
    with open(data_path, 'r', encoding='utf-8') as f:
        data = json.load(f)


    for index, example in tqdm(enumerate(data), total=len(data)):
        if index < len(data_new):
            continue
        
        nlq = example['nlq']
        MQL = example['text2nosql_pred']
        query_collection = example['query_collection_pred'].split(", ")
        fields_db = example['db_fields_pred'].split(", ")
        fields_alias = example['alias_fields_pred'].split(", ")
        target_fields = example['target_fields_pred'].split(", ")
        
        query_collection.sort()
        fields_db.sort()
        fields_alias.sort()
        target_fields.sort()

        query_collection = ", ".join(query_collection)
        fields_db = ", ".join(fields_db)
        fields_alias = ", ".join(fields_alias)
        target_fields = ", ".join(target_fields)

        nlq_emb = get_embedding(nlq)
        MQL_emb = get_embedding(MQL)
        query_collection_emb = get_embedding(query_collection)
        fields_db_emb = get_embedding(fields_db)
        fields_alias_emb = get_embedding(fields_alias)
        target_fields_emb = get_embedding(target_fields)

        
        nlq_emb = get_embedding(example['nlq'])
        
        example_new = {
            "nlq":{"value":nlq, "embedding":nlq_emb},
            "db_id":example['db_id'],
            "mql":{"value":MQL, "embedding":MQL_emb},
            "fields_db":{"value":fields_db, "embedding":fields_db_emb},
            "fields_alias":{"value":fields_alias, "embedding":fields_alias_emb},
            "target_fields":{"value":target_fields, "embedding":target_fields_emb},
            "query_collection":{"value":query_collection, "embedding":query_collection_emb}
        }

        data_new.append(example_new)

        if index % 10 == 0:
            with open(vql_embedding_file_path, 'wb') as f:
                pickle.dump(data_new, f)
    
    with open(vql_embedding_file_path, 'wb') as f:
        pickle.dump(data_new, f)