import openai
import numpy as np
from scipy.spatial.distance import cosine
import json
import pandas as pd
from tqdm import tqdm
import pickle
import time
import os


train_emb_path = "./vector_store/train.pkl"
test_emb_path = "./vector_store/test.pkl"


api_base = "https://openkey.cloud/v1"

# 替换为你的OpenAI API密钥
api_key = "sk-u4QlUZW8nLtaiqyv40338f916f5a4d6c81B42aB83b6fE567"

client = openai.Client(api_key=api_key, base_url=api_base)

cache = {}

with open(train_emb_path, 'rb') as f:
    data_embedding_all = pickle.load(f)

data_embedding_all_pd = pd.DataFrame(data_embedding_all)
data_embeddings = data_embedding_all_pd.to_dict(orient='records')

def get_embedding(text, model="text-embedding-ada-002"):
    if text in cache:
        return cache[text]
    embedding = client.embeddings.create(input=text, model=model)
    cache[text] = np.array(embedding.data[0].embedding)
    return np.array(embedding.data[0].embedding)


def rag_by_nlq_pref(nlq_emb, rough_mql_emb, fields_db_emb, fields_alias_emb, target_fields_emb, collection_emb, k=1) -> list:

    # 计算问题向量与文档中每个句子向量的相似度
    similarities = []
    for example_emb in data_embeddings:
        nlq_sim = 1 - cosine(example_emb['nlq']['embedding'], nlq_emb)
        mql_sim = 1 - cosine(example_emb['mql']['embedding'], rough_mql_emb)
        fields_db_sim = 1 - cosine(example_emb['fields_db']['embedding'], fields_db_emb)
        fields_alias_sim = 1 - cosine(example_emb['fields_alias']['embedding'], fields_alias_emb)
        target_fields_sim = 1 - cosine(example_emb['target_fields']['embedding'], target_fields_emb)
        collection_sim = 1 - cosine(example_emb['query_collection']['embedding'], collection_emb)

        simi = nlq_sim*1 + mql_sim*0.3 + fields_db_sim*0.7 + fields_alias_sim*0.5 + target_fields_sim*0.5 + collection_sim*0.7
        
        similarities.append(simi)

    # 选择top-k个最相似的句子
    top_k_indices = np.argsort(similarities)[-k:][::-1]
    top_k_row = data_embedding_all_pd.loc[top_k_indices.tolist()][['nlq', 'mql', 'fields_db', 'fields_alias', 'target_fields', 'db_id', 'query_collection']]

    examples = []
    for index, row in top_k_row.iterrows():
            example = {
                "db_id":row['db_id'],
                "NLQ":row['nlq']['value'],
                "MQL":row['mql']['value'],
                "fields_db":row['fields_db']['value'],
                "fields_alias":row['fields_alias']['value'],
                "target_fields":row['target_fields']['value'],
                "query_collection":row['query_collection']['value']
            }
            examples.append(example)

    return examples

def rag_by_nlq(nlq_emb, k=1) -> list:

    # 计算问题向量与文档中每个句子向量的相似度
    similarities = []
    for example_emb in data_embeddings:
        nlq_sim = 1 - cosine(example_emb['nlq']['embedding'], nlq_emb)

        simi = nlq_sim*1
        
        similarities.append(simi)

    # 选择top-k个最相似的句子
    top_k_indices = np.argsort(similarities)[-k:][::-1]
    top_k_row = data_embedding_all_pd.loc[top_k_indices.tolist()][['nlq', 'mql', 'db_id']]

    examples = []
    for index, row in top_k_row.iterrows():
            example = {
                "db_id":row['db_id'],
                "NLQ":row['nlq']['value'],
                "MQL":row['mql']['value']
            }
            examples.append(example)

    return examples

if __name__ == '__main__':
    data_path = "./TEND/test_SLM_prediction.json"
    result_save_path = "./TEND/test_SLM_prediction_rag_no_pref.json"
    

    data_new = []
    if os.path.exists(result_save_path):
        with open(result_save_path, 'r', encoding='utf-8') as f: 
            data_new = json.load(f)
    with open(test_emb_path, 'rb') as f:
        data = pickle.load(f)

    with open(data_path, "r", encoding='utf-8') as f:
        test_data = json.load(f)
    
    for index, (example, example_test) in tqdm(enumerate(zip(data, test_data)), total=len(data)):
        if index < len(data_new):
            continue

        nlq_emb = example['nlq']['embedding']
        
        rag_examples = rag_by_nlq(nlq_emb, k=20)
        example_new = example_test.copy()

        example_new['RAG_examples'] = rag_examples

        data_new.append(example_new)

        if index % 20 == 0:
            with open(result_save_path, 'w') as f:
                json.dump(data_new, f, indent=4)

    with open(result_save_path, 'w') as f:
        json.dump(data_new, f, indent=4)
        