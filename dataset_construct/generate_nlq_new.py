import json
import os
from example_prompt import ask_nlq_mql, ans_nlq_mql
from utils import execute_sql, generate_reply, generate_claude_reply, schemas_transform

data_save_path = "./text2MongoDB_dataset/dataset_final.json"
schema_folder_path = "./mongodb_schema/"

ask1 = """# Given MongoDB collections with their fields, a MongoDB query, and a gold natural language queries, please perform the following steps:
1. Analyze the gold natural language queries based on the MongoDB query and the MongoDB collections with their fields;
2. Expand 3 new natural language queries that have the same meaning but different expressions from the gold natural language queries based on the analysis in step 1;
3. Ensure that the 3 newly expanded natural language queries have different expressions but the same meaning as the gold natural language queries;
4. Ensure the 3 natural language queries are distinct from each other and are expressed naturally, confluently and concisely;
5. Output the newly expanded natural language queries in the following format:

```markdown
1. **Natural Language Query 1**: [NLQ1]
2. **Natural Language Query 2**: [NLQ2]
...
```

## MongoDB Collections with their Fields in chinook_1 Database
### Artist: ArtistId, Name, Album.AlbumId, Album.Title, Album.ArtistId, Album.Track.TrackId, Album.Track.Name, Album.Track.AlbumId, Album.Track.MediaTypeId, Album.Track.GenreId, Album.Track.Composer, Album.Track.Milliseconds, Album.Track.Bytes, Album.Track.UnitPrice, Album.Track.InvoiceLine.InvoiceLineId, Album.Track.InvoiceLine.InvoiceId, Album.Track.InvoiceLine.TrackId, Album.Track.InvoiceLine.UnitPrice, Album.Track.InvoiceLine.Quantity, Album.Track.PlaylistTrack.PlaylistId, Album.Track.PlaylistTrack.TrackId
### Employee: EmployeeId, LastName, FirstName, Title, ReportsTo, BirthDate, HireDate, Address, City, State, Country, PostalCode, Phone, Fax, Email, Customer.CustomerId, Customer.FirstName, Customer.LastName, Customer.Company, Customer.Address, Customer.City, Customer.State, Customer.Country, Customer.PostalCode, Customer.Phone, Customer.Fax, Customer.Email, Customer.SupportRepId, Customer.Invoice.InvoiceId, Customer.Invoice.CustomerId, Customer.Invoice.InvoiceDate, Customer.Invoice.BillingAddress, Customer.Invoice.BillingCity, Customer.Invoice.BillingState, Customer.Invoice.BillingCountry, Customer.Invoice.BillingPostalCode, Customer.Invoice.Total, Customer.Invoice.InvoiceLine.InvoiceLineId, Customer.Invoice.InvoiceLine.InvoiceId, Customer.Invoice.InvoiceLine.TrackId, Customer.Invoice.InvoiceLine.UnitPrice, Customer.Invoice.InvoiceLine.Quantity
### Playlist: PlaylistId, Name, PlaylistTrack.PlaylistId, PlaylistTrack.TrackId
### MediaType: MediaTypeId, Name, Track.TrackId, Track.Name, Track.AlbumId, Track.MediaTypeId, Track.GenreId, Track.Composer, Track.Milliseconds, Track.Bytes, Track.UnitPrice, Track.InvoiceLine.InvoiceLineId, Track.InvoiceLine.InvoiceId, Track.InvoiceLine.TrackId, Track.InvoiceLine.UnitPrice, Track.InvoiceLine.Quantity, Track.PlaylistTrack.PlaylistId, Track.PlaylistTrack.TrackId
### Genre: GenreId, Name, Track.TrackId, Track.Name, Track.AlbumId, Track.MediaTypeId, Track.GenreId, Track.Composer, Track.Milliseconds, Track.Bytes, Track.UnitPrice, Track.InvoiceLine.InvoiceLineId, Track.InvoiceLine.InvoiceId, Track.InvoiceLine.TrackId, Track.InvoiceLine.UnitPrice, Track.InvoiceLine.Quantity, Track.PlaylistTrack.PlaylistId, Track.PlaylistTrack.TrackId

## MongoDB Query

```javascript
db.Artist.aggregate([
  { $unwind: "$Album" },
  { $unwind: "$Album.Track" },
  {
    $lookup: {
      from: "Genre",
      localField: "Album.Track.GenreId",
      foreignField: "GenreId",
      as: "Docs1"
    }
  },
  { $unwind: "$Docs1" },
  {
    $match: {
      "Docs1.Name": "Jazz"
    }
  },
  {
    $group: {
      _id: null,
      avg_UnitPrice: { $avg: "$Album.Track.UnitPrice" }
    }
  },
  {
    $project: {
      _id: 0,
      avg_UnitPrice: 1
    }
  }
])
```

## Gold Natural Language Queries

```markdown
1. **Gold Natural Language Query 1**: What is the average unit price of tracks that belong to Jazz genre?
2. **Gold Natural Language Query 2**: Find the average unit price of jazz tracks.
```

A: Let's think step by step! """

ans1 = """To perform the task at hand, we start by analyzing the given MongoDB query and gold natural language queries:

### Analysis

1. **MongoDB Query Analysis**:
   - The query operates over the `Artist` collection.
   - It unwinds embedded `Album` and `Track` arrays to process each track individually.
   - It performs a lookup (join) with the `Genre` collection to associate each track with its genre.
   - The query filters tracks to only consider those belonging to the "Jazz" genre.
   - It calculates the average of the `UnitPrice` of the filtered tracks.
   - Finally, it projects the calculated average unit price.

2. **Gold Natural Language Queries Analysis**:
   - Both natural language queries aim to derive the average unit price of music tracks that are categorized under the "Jazz" genre.
   - The queries focus on the "Jazz" genre and are centered on calculating an average, reflecting the operations in the MongoDB query.

### Expansion of Natural Language Queries

Based on the analysis, we can create three new natural language queries that vary in expression but convey the same meaning as the gold queries:

```markdown
1. **Natural Language Query 1**: Calculate the mean unit price for tracks classified under the Jazz genre.
2. **Natural Language Query 2**: What is the average cost per unit of music tracks in the Jazz category?
3. **Natural Language Query 3**: Determine the average unit pricing for tracks that fall within the Jazz music genre.
```

Each of these queries expresses the task of computing the average unit price of tracks categorized under Jazz, while maintaining syntactic variety."""

def prompt_maker(nlqs:list, ref_sql:str, db_id:str, mql:str):
    
    # schemas_sql = execute_sql(db_name, ref_sql)['schemas']
    # schemas_sql = ", ".join(schemas_sql)
    num = 5 - len(nlqs)
    if num <= 0:
        return nlqs
    instruction = """# Given MongoDB collections with their fields, a MongoDB query, and a gold natural language queries, please perform the following steps:
1. Analyze the gold natural language queries based on the MongoDB query and the MongoDB collections with their fields;
2. Expand {num} new natural language queries that have the same meaning but different expressions from the gold natural language queries based on the analysis in step 1;
3. Ensure that the {num} newly expanded natural language queries have different expressions but the same meaning as the gold natural language queries;
4. Ensure the {num} natural language queries are distinct from each other and are expressed naturally, confluently and concisely;
5. Output the newly expanded natural language queries in the following format:

```markdown
1. **Natural Language Query 1**: [NLQ1]
2. **Natural Language Query 2**: [NLQ2]
...
```""".format(num=num)
    schema_prompt = schemas_transform(db_id=db_id)
    nlqs_str = ""
    for id, nlq in enumerate(nlqs):
        nlqs_str += f"{id+1}. **Gold Natural Language Query {id+1}**: {nlq}\n"

    nlqs_str = nlqs_str.strip()
    mql = mql.strip()
    schema_prompt = schema_prompt.strip("")

    prompt = f"""{instruction}

{schema_prompt.strip()}

## MongoDB Query

```javascript
{mql}
```

## Gold Natural Language Queries

```markdown
{nlqs_str}
```

A: Let's think step by step! """
    return prompt



def generale_nlq_by_mql(nlqs:list, ref_sql:str, db_name:str, mql:str, model:str, n=1):

    prompt = prompt_maker(nlqs, ref_sql, db_name, mql)

    messages = [
        {
            "role": "user",
            "content": ask1,
        },
        {
            "role": "assistant",
            "content": ans1
        },
        {
            "role":"user",
            "content":prompt
        }
    ]
    reply = None
    while reply == None:
        try:
            if "gpt" in model:
                reply = generate_reply(messages=messages, model=model, temperature=0.0)
            elif "claude" in model:
                reply = generate_claude_reply(messages=messages, model=model)
            else:
                raise TypeError("Don't support model type {}".format(model))

            nlq = reply.split("```markdown", 1)[1].split("```", 1)[0].strip().split("\n")
            nlq = [row.split(":", 1)[1].strip() for row in nlq]
        except Exception as e:
            print(e)
            reply = None
    
    return nlq


if __name__ == "__main__":
    ref_sql = "SELECT COUNT(*) FROM CARS_DATA WHERE Cylinders > 6;"
    nlqs = [
            "How many cars has over 6 cylinders?",
            "What is the number of carsw ith over 6 cylinders?"
    ]
    db_name = "car_1"

    mql = "db.continents.aggregate([\n  {\n    $unwind: \"$countries\"\n  },\n  {\n    $unwind: \"$countries.car_makers\"\n  },\n  {\n    $unwind: \"$countries.car_makers.model_list\"\n  },\n  {\n    $unwind: \"$countries.car_makers.model_list.car_names\"\n  },\n  {\n    $unwind: \"$countries.car_makers.model_list.car_names.cars_data\"\n  },\n  {\n    $match: {\n      \"countries.car_makers.model_list.car_names.cars_data.Cylinders\": {\n        $gt: 6\n      }\n    }\n  },\n  {\n    $group: {\n      _id: null,\n      count: { $sum: 1 }\n    }\n  },\n  {\n    $project: {\n      _id: 0,\n      COUNT: \"$count\"\n    }\n  }\n]);\n"


    # prompt = prompt_maker(schemas, nlqs, ref_sql, db_name, mql)

    # print(prompt)

    nlq = generale_nlq_by_mql(nlqs, ref_sql, db_name, mql, "gpt-4o-mini")
    print(nlq)

    # if "```" in nlq:
    #     nlq = nlq.split("```", 2)[1].replace("\n", "")
        

    # print(nlq)