import json
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply

SYSTEM_PROMPT = "You are now a natural language interface for a MongoDB database, responsible for converting natural language queries into MongoDB queries."

ICL_PROMPT = """# Given the MongoDB schemas, please convert the following natural language queries into MongoDB queries.


## Natural Language Query: `List all procedures that cost under 5000 and in which physician John Wen received training.`
## MongoDB Schema
```markdown
### Collection: Physician
- EmployeeID: INTEGER
- Name: VARCHAR(30)
- Position: VARCHAR(30)
- SSN: INTEGER
- Department (Array):
  - DepartmentID: INTEGER
  - Name: VARCHAR(30)
  - Head: INTEGER
  - Affiliated_With (Array):
    - Physician: INTEGER
    - Department: INTEGER
    - PrimaryAffiliation: BOOLEAN
- Affiliated_With (Array):
  - Physician: INTEGER
  - Department: INTEGER
  - PrimaryAffiliation: BOOLEAN
- Trained_In (Array):
  - Physician: INTEGER
  - Treatment: INTEGER
  - CertificationDate: DATETIME
  - CertificationExpires: DATETIME
- Patient (Array):
  - SSN: INTEGER
  - Name: VARCHAR(30)
  - Address: VARCHAR(30)
  - Phone: VARCHAR(30)
  - InsuranceID: INTEGER
  - PCP: INTEGER
  - Appointment (Array):
    - AppointmentID: INTEGER
    - Patient: INTEGER
    - PrepNurse: INTEGER
    - Physician: INTEGER
    - Start: DATETIME
    - End: DATETIME
    - ExaminationRoom: TEXT
    - Prescribes (Array):
      - Physician: INTEGER
      - Patient: INTEGER
      - Medication: INTEGER
      - Date: DATETIME
      - Appointment: INTEGER
      - Dose: VARCHAR(30)
  - Prescribes (Array):
    - Physician: INTEGER
    - Patient: INTEGER
    - Medication: INTEGER
    - Date: DATETIME
    - Appointment: INTEGER
    - Dose: VARCHAR(30)
  - Stay (Array):
    - StayID: INTEGER
    - Patient: INTEGER
    - Room: INTEGER
    - StayStart: DATETIME
    - StayEnd: DATETIME
    - Undergoes (Array):
      - Patient: INTEGER
      - Procedures: INTEGER
      - Stay: INTEGER
      - DateUndergoes: DATETIME
      - Physician: INTEGER
      - AssistingNurse: INTEGER
  - Undergoes (Array):
    - Patient: INTEGER
    - Procedures: INTEGER
    - Stay: INTEGER
    - DateUndergoes: DATETIME
    - Physician: INTEGER
    - AssistingNurse: INTEGER
- Appointment (Array):
  - AppointmentID: INTEGER
  - Patient: INTEGER
  - PrepNurse: INTEGER
  - Physician: INTEGER
  - Start: DATETIME
  - End: DATETIME
  - ExaminationRoom: TEXT
  - Prescribes (Array):
    - Physician: INTEGER
    - Patient: INTEGER
    - Medication: INTEGER
    - Date: DATETIME
    - Appointment: INTEGER
    - Dose: VARCHAR(30)
- Prescribes (Array):
  - Physician: INTEGER
  - Patient: INTEGER
  - Medication: INTEGER
  - Date: DATETIME
  - Appointment: INTEGER
  - Dose: VARCHAR(30)
- Undergoes (Array):
  - Patient: INTEGER
  - Procedures: INTEGER
  - Stay: INTEGER
  - DateUndergoes: DATETIME
  - Physician: INTEGER
  - AssistingNurse: INTEGER

### Collection: Procedures
- Code: INTEGER
- Name: VARCHAR(30)
- Cost: REAL
- Trained_In (Array):
  - Physician: INTEGER
  - Treatment: INTEGER
  - CertificationDate: DATETIME
  - CertificationExpires: DATETIME
- Undergoes (Array):
  - Patient: INTEGER
  - Procedures: INTEGER
  - Stay: INTEGER
  - DateUndergoes: DATETIME
  - Physician: INTEGER
  - AssistingNurse: INTEGER

### Collection: Nurse
- EmployeeID: INTEGER
- Name: VARCHAR(30)
- Position: VARCHAR(30)
- Registered: BOOLEAN
- SSN: INTEGER
- Appointment (Array):
  - AppointmentID: INTEGER
  - Patient: INTEGER
  - PrepNurse: INTEGER
  - Physician: INTEGER
  - Start: DATETIME
  - End: DATETIME
  - ExaminationRoom: TEXT
  - Prescribes (Array):
    - Physician: INTEGER
    - Patient: INTEGER
    - Medication: INTEGER
    - Date: DATETIME
    - Appointment: INTEGER
    - Dose: VARCHAR(30)
- On_Call (Array):
  - Nurse: INTEGER
  - BlockFloor: INTEGER
  - BlockCode: INTEGER
  - OnCallStart: DATETIME
  - OnCallEnd: DATETIME
- Undergoes (Array):
  - Patient: INTEGER
  - Procedures: INTEGER
  - Stay: INTEGER
  - DateUndergoes: DATETIME
  - Physician: INTEGER
  - AssistingNurse: INTEGER

### Collection: Medication
- Code: INTEGER
- Name: VARCHAR(30)
- Brand: VARCHAR(30)
- Description: VARCHAR(30)
- Prescribes (Array):
  - Physician: INTEGER
  - Patient: INTEGER
  - Medication: INTEGER
  - Date: DATETIME
  - Appointment: INTEGER
  - Dose: VARCHAR(30)

### Collection: Block
- BlockFloor: INTEGER
- BlockCode: INTEGER
- Room (Array):
  - RoomNumber: INTEGER
  - RoomType: VARCHAR(30)
  - BlockFloor: INTEGER
  - BlockCode: INTEGER
  - Unavailable: BOOLEAN
  - Stay (Array):
    - StayID: INTEGER
    - Patient: INTEGER
    - Room: INTEGER
    - StayStart: DATETIME
    - StayEnd: DATETIME
    - Undergoes (Array):
      - Patient: INTEGER
      - Procedures: INTEGER
      - Stay: INTEGER
      - DateUndergoes: DATETIME
      - Physician: INTEGER
      - AssistingNurse: INTEGER
- On_Call (Array):
  - Nurse: INTEGER
  - BlockFloor: INTEGER
  - BlockCode: INTEGER
  - OnCallStart: DATETIME
  - OnCallEnd: DATETIME
```

## MongoDB Query:
```javascript
db.Procedures.aggregate([
  {
    $match: {
      Cost: { $lt: 5000 }
    }
  },
  {
    $lookup: {
      from: "Physician",
      localField: "Trained_In.Physician",
      foreignField: "EmployeeID",
      as: "Docs1"
    }
  },
  {
    $match: {
      "Docs1.Name": "John Wen"
    }
  },
  {
    $project: {
      Name: 1,
      _id: 0
    }
  }
]);
```


## NLQ: `What is the name and building of the departments whose budget is more than the average budget?`
## MongoDB Schema
```markdown
### Collection: department
- dept_name: varchar(20)
- building: varchar(15)
- budget: numeric(12,2)
- course (Array):
  - course_id: varchar(8)
  - title: varchar(50)
  - dept_name: varchar(20)
  - credits: numeric(2,0)
  - section (Array):
    - course_id: varchar(8)
    - sec_id: varchar(8)
    - semester: varchar(6)
    - year: numeric(4,0)
    - building: varchar(15)
    - room_number: varchar(7)
    - time_slot_id: varchar(4)
    - teaches (Array):
      - ID: varchar(5)
      - course_id: varchar(8)
      - sec_id: varchar(8)
      - semester: varchar(6)
      - year: numeric(4,0)
    - takes (Array):
      - ID: varchar(5)
      - course_id: varchar(8)
      - sec_id: varchar(8)
      - semester: varchar(6)
      - year: numeric(4,0)
      - grade: varchar(2)
  - prereq (Array):
    - course_id: varchar(8)
    - prereq_id: varchar(8)
- instructor (Array):
  - ID: varchar(5)
  - name: varchar(20)
  - dept_name: varchar(20)
  - salary: numeric(8,2)
  - teaches (Array):
    - ID: varchar(5)
    - course_id: varchar(8)
    - sec_id: varchar(8)
    - semester: varchar(6)
    - year: numeric(4,0)
  - advisor (Array):
    - s_ID: varchar(5)
    - i_ID: varchar(5)
- student (Array):
  - ID: varchar(5)
  - name: varchar(20)
  - dept_name: varchar(20)
  - tot_cred: numeric(3,0)
  - takes (Array):
    - ID: varchar(5)
    - course_id: varchar(8)
    - sec_id: varchar(8)
    - semester: varchar(6)
    - year: numeric(4,0)
    - grade: varchar(2)
  - advisor (Array):
    - s_ID: varchar(5)
    - i_ID: varchar(5)

### Collection: classroom
- building: varchar(15)
- room_number: varchar(7)
- capacity: numeric(4,0)
- section (Array):
  - course_id: varchar(8)
  - sec_id: varchar(8)
  - semester: varchar(6)
  - year: numeric(4,0)
  - building: varchar(15)
  - room_number: varchar(7)
  - time_slot_id: varchar(4)
  - teaches (Array):
    - ID: varchar(5)
    - course_id: varchar(8)
    - sec_id: varchar(8)
    - semester: varchar(6)
    - year: numeric(4,0)
  - takes (Array):
    - ID: varchar(5)
    - course_id: varchar(8)
    - sec_id: varchar(8)
    - semester: varchar(6)
    - year: numeric(4,0)
    - grade: varchar(2)

### Collection: time_slot
- time_slot_id: varchar(4)
- day: varchar(1)
- start_hr: numeric(2)
- start_min: numeric(2)
- end_hr: numeric(2)
- end_min: numeric(2)
```

## MongoDB Query:
```javascript
db.department.aggregate([
  {
    $group: {
      _id: null,
      avg_budget: { $avg: "$budget" }
    }
  },
  {
    $lookup: {
      from: "department",
      let: { avg_budget: "$avg_budget" },
      pipeline: [
        {
          $match: {
            $expr: { $gt: ["$budget", "$$avg_budget"] }
          }
        },
        {
          $project: {
            _id: 0,
            dept_name: 1,
            building: 1
          }
        }
      ],
      as: "Docs1"
    }
  },
  {
    $unwind: "$Docs1"
  },
  {
    $replaceRoot: { newRoot: "$Docs1" }
  }
]);
```


## Natural Language Query: `Provide a sorted list of all customers based on their IDs in ascending order.`
## MongoDB Schema
```markdown
### Collection: Available_Policies
- Policy_ID: INTEGER
- policy_type_code: CHAR(15)
- Customer_Phone: VARCHAR(255)
- Customers_Policies (Array):
  - Customer_ID: INTEGER
  - Policy_ID: INTEGER
  - Date_Opened: DATE
  - Date_Closed: DATE
  - First_Notification_of_Loss (Array):
    - FNOL_ID: INTEGER
    - Customer_ID: INTEGER
    - Policy_ID: INTEGER
    - Service_ID: INTEGER
    - Claims (Array):
      - Claim_ID: INTEGER
      - FNOL_ID: INTEGER
      - Effective_Date: DATE
      - Settlements (Array):
        - Settlement_ID: INTEGER
        - Claim_ID: INTEGER
        - Effective_Date: DATE
        - Settlement_Amount: REAL

### Collection: Customers
- Customer_ID: INTEGER
- Customer_name: VARCHAR(40)
- Customers_Policies (Array):
  - Customer_ID: INTEGER
  - Policy_ID: INTEGER
  - Date_Opened: DATE
  - Date_Closed: DATE
  - First_Notification_of_Loss (Array):
    - FNOL_ID: INTEGER
    - Customer_ID: INTEGER
    - Policy_ID: INTEGER
    - Service_ID: INTEGER
    - Claims (Array):
      - Claim_ID: INTEGER
      - FNOL_ID: INTEGER
      - Effective_Date: DATE
      - Settlements (Array):
        - Settlement_ID: INTEGER
        - Claim_ID: INTEGER
        - Effective_Date: DATE
        - Settlement_Amount: REAL

### Collection: Services
- Service_ID: INTEGER
- Service_name: VARCHAR(40)
- First_Notification_of_Loss (Array):
  - FNOL_ID: INTEGER
  - Customer_ID: INTEGER
  - Policy_ID: INTEGER
  - Service_ID: INTEGER
  - Claims (Array):
    - Claim_ID: INTEGER
    - FNOL_ID: INTEGER
    - Effective_Date: DATE
    - Settlements (Array):
      - Settlement_ID: INTEGER
      - Claim_ID: INTEGER
      - Effective_Date: DATE
      - Settlement_Amount: REAL
```

## MongoDB Query:
```javascript
db.Customers.find({}, { Customer_ID: 1, Customer_name: 1, _id: 0 }).sort({ Customer_ID: 1 });
```


## Natural Language Query: `What are the names of all the clubs starting with the oldest?`
## MongoDB Schema
```markdown
### Collection: club
- Club_ID: INT
- name: TEXT
- Region: TEXT
- Start_year: TEXT
- club_rank (Array):
  - Rank: REAL
  - Club_ID: INT
  - Gold: REAL
  - Silver: REAL
  - Bronze: REAL
  - Total: REAL
- player (Array):
  - Player_ID: INT
  - name: TEXT
  - Position: TEXT
  - Club_ID: INT
  - Apps: REAL
  - Tries: REAL
  - Goals: TEXT
  - Points: REAL
- competition_result (Array):
  - Competition_ID: INT
  - Club_ID_1: INT
  - Club_ID_2: INT
  - Score: TEXT

### Collection: competition
- Competition_ID: INT
- Year: REAL
- Competition_type: TEXT
- Country: TEXT
- competition_result (Array):
  - Competition_ID: INT
  - Club_ID_1: INT
  - Club_ID_2: INT
  - Score: TEXT
```

## MongoDB Query:
```javascript
db.club.find({}, { name: 1, _id: 0 }).sort({ Start_year: 1 }).limit(1);
```


### NLQ: “Which clubs have one or more members from the city with code "HOU"? Give me the names of the clubs.”
## MongoDB Schema
```markdown
### Collection: Club
- ClubID: INTEGER
- ClubName: VARCHAR(40)
- ClubDesc: VARCHAR(1024)
- ClubLocation: VARCHAR(40)
- Member_of_club (Array):
  - StuID: INTEGER
  - ClubID: INTEGER
  - Position: VARCHAR(40)

### Collection: Student
- StuID: INTEGER
- LName: VARCHAR(12)
- Fname: VARCHAR(12)
- Age: INTEGER
- Sex: VARCHAR(1)
- Major: INTEGER
- Advisor: INTEGER
- city_code: VARCHAR(3)
- Member_of_club (Array):
  - StuID: INTEGER
  - ClubID: INTEGER
  - Position: VARCHAR(40)
```

## MongoDB Query:
```javascript
db.Club.aggregate([
  {
    $match: {
      ClubName: "Hopkins Student Enterprises"
    }
  },
  {
    $lookup: {
      from: "Student",
      localField: "ClubID",
      foreignField: "Member_of_club.ClubID",
      as: "Docs1"
    }
  },
  {
    $unwind: "$Docs1"
  },
  {
    $project: {
      LName: "$Docs1.LName",
      _id: 0
    }
  }
]);
```"""

def prompt_maker(example:dict, if_print:bool):
    # prompt = ICL_PROMPT + "\n"
    prompt = ICL_PROMPT
    NLQ = example['nlq']
    db_id = example['db_id']
        
    prompt += """

## Natural Language Query: `{}`
## MongoDB Schema
```markdown
{}
```

## MongoDB Query:
""".format(NLQ, schemas_transform(db_id=db_id))
    
    if if_print:
        print(prompt)
    return prompt

def generate_icl(example:dict):
    prompt = prompt_maker(example, False)


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
    save_path = "./results/results_icl_deepseekv3.json"

    with open(file_name, "r") as f:
        test_data = json.load(f)

    results = []
    for example in tqdm(test_data, total=len(test_data)):

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