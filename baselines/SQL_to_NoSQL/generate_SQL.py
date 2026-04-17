import json
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply

SYSTEM_PROMPT = "You are now a natural language interface for a MongoDB database, responsible for converting natural language queries into MongoDB queries."

ICL_PROMPT = """# Given the MongoDB schemas, please convert the following natural language queries into MongoDB queries.


## Natural Language Query: `Find the name of physicians whose position title contains the word 'senior'.`
## Database Schemas
```markdown
### Table: Physician
#### Columns: EmployeeID, Name, Position, SSN, Department_DepartmentID, Department_Name, Department_Head, Department_Affiliated_With_Physician, Department_Affiliated_With_Department, Department_Affiliated_With_PrimaryAffiliationAffiliated_With_Physician, Affiliated_With_Department, Affiliated_With_PrimaryAffiliation,Trained_In_Physician, Trained_In_Treatment, Trained_In_CertificationDate, Trained_In_CertificationExpires,Patient_SSN, Patient_Name, Patient_Address, Patient_Phone, Patient_InsuranceID, Patient_PCP, Patient_Appointment_AppointmentID, Patient_Appointment_Patient, Patient_Appointment_PrepNurse, Patient_Appointment_Physician, Patient_Appointment_Start, Patient_Appointment_End, Patient_Appointment_ExaminationRoom, Patient_Appointment_Prescribes_Physician, Patient_Appointment_Prescribes_Patient, Patient_Appointment_Prescribes_Medication, Patient_Appointment_Prescribes_Date, Patient_Appointment_Prescribes_Appointment, Patient_Appointment_Prescribes_DosePatient_Prescribes_Physician, Patient_Prescribes_Patient, Patient_Prescribes_Medication, Patient_Prescribes_Date, Patient_Prescribes_Appointment, Patient_Prescribes_Dose,Patient_Stay_StayID, Patient_Stay_Patient, Patient_Stay_Room, Patient_Stay_StayStart, Patient_Stay_StayEnd, Patient_Stay_Undergoes_Patient, Patient_Stay_Undergoes_Procedures, Patient_Stay_Undergoes_Stay, Patient_Stay_Undergoes_DateUndergoes, Patient_Stay_Undergoes_Physician, Patient_Stay_Undergoes_AssistingNursePatient_Undergoes_Patient, Patient_Undergoes_Procedures, Patient_Undergoes_Stay, Patient_Undergoes_DateUndergoes, Patient_Undergoes_Physician, Patient_Undergoes_AssistingNurseAppointment_AppointmentID, Appointment_Patient, Appointment_PrepNurse, Appointment_Physician, Appointment_Start, Appointment_End, Appointment_ExaminationRoom, Appointment_Prescribes_Physician, Appointment_Prescribes_Patient, Appointment_Prescribes_Medication, Appointment_Prescribes_Date, Appointment_Prescribes_Appointment, Appointment_Prescribes_DosePrescribes_Physician, Prescribes_Patient, Prescribes_Medication, Prescribes_Date, Prescribes_Appointment, Prescribes_Dose,Undergoes_Patient, Undergoes_Procedures, Undergoes_Stay, Undergoes_DateUndergoes, Undergoes_Physician, Undergoes_AssistingNurse
### Table: Procedures
#### Columns: Code, Name, Cost, Trained_In_Physician, Trained_In_Treatment, Trained_In_CertificationDate, Trained_In_CertificationExpires,Undergoes_Patient, Undergoes_Procedures, Undergoes_Stay, Undergoes_DateUndergoes, Undergoes_Physician, Undergoes_AssistingNurse
### Table: Nurse
#### Columns: EmployeeID, Name, Position, Registered, SSN, Appointment_AppointmentID, Appointment_Patient, Appointment_PrepNurse, Appointment_Physician, Appointment_Start, Appointment_End, Appointment_ExaminationRoom, Appointment_Prescribes_Physician, Appointment_Prescribes_Patient, Appointment_Prescribes_Medication, Appointment_Prescribes_Date, Appointment_Prescribes_Appointment, Appointment_Prescribes_DoseOn_Call_Nurse, On_Call_BlockFloor, On_Call_BlockCode, On_Call_OnCallStart, On_Call_OnCallEnd,Undergoes_Patient, Undergoes_Procedures, Undergoes_Stay, Undergoes_DateUndergoes, Undergoes_Physician, Undergoes_AssistingNurse
### Table: Medication
#### Columns: Code, Name, Brand, Description, Prescribes_Physician, Prescribes_Patient, Prescribes_Medication, Prescribes_Date, Prescribes_Appointment, Prescribes_Dose
### Table: Block
#### Columns: BlockFloor, BlockCode, Room_RoomNumber, Room_RoomType, Room_BlockFloor, Room_BlockCode, Room_Unavailable, Room_Stay_StayID, Room_Stay_Patient, Room_Stay_Room, Room_Stay_StayStart, Room_Stay_StayEnd, Room_Stay_Undergoes_Patient, Room_Stay_Undergoes_Procedures, Room_Stay_Undergoes_Stay, Room_Stay_Undergoes_DateUndergoes, Room_Stay_Undergoes_Physician, Room_Stay_Undergoes_AssistingNursOn_Call_Nurse, On_Call_BlockFloor, On_Call_BlockCode, On_Call_OnCallStart, On_Call_OnCallEnd
```

## SQL Query:
```sql
SELECT Name FROM Physician WHERE Physician LIKE '%senior%'
```


## NLQ: `What is the name and building of the departments whose budget is more than the average budget?`
## MongoDB Schema
```markdown
### Table: department
#### Columns: dept_name, building, budget, course, course_course_id, course_title, course_dept_name, course_credits, course_section, course_section_course_id, course_section_sec_id, course_section_semester, course_section_year, course_section_building, course_section_room_number, course_section_time_slot_id, course_section_teaches, course_section_teaches_ID, course_section_teaches_course_id, course_section_teaches_sec_id, course_section_teaches_semester, course_section_teaches_year,course_section_takes, course_section_takes_ID, course_section_takes_course_id, course_section_takes_sec_id, course_section_takes_semester, course_section_takes_year, course_section_takes_gradecourse_prereq, course_prereq_course_id, course_prereq_prereq_idinstructor, instructor_ID, instructor_name, instructor_dept_name, instructor_salary, instructor_teaches, instructor_teaches_ID, instructor_teaches_course_id, instructor_teaches_sec_id, instructor_teaches_semester, instructor_teaches_year,instructor_advisor, instructor_advisor_s_ID, instructor_advisor_i_IDstudent, student_ID, student_name, student_dept_name, student_tot_cred, student_takes, student_takes_ID, student_takes_course_id, student_takes_sec_id, student_takes_semester, student_takes_year, student_takes_grade,student_advisor, student_advisor_s_ID, student_advisor_i_I
### Table: classroom
#### Columns: building, room_number, capacity, section, section_course_id, section_sec_id, section_semester, section_year, section_building, section_room_number, section_time_slot_id, section_teaches, section_teaches_ID, section_teaches_course_id, section_teaches_sec_id, section_teaches_semester, section_teaches_year,section_takes, section_takes_ID, section_takes_course_id, section_takes_sec_id, section_takes_semester, section_takes_year, section_takes_grad
### Table: time_slot
#### Columns: time_slot_id, day, start_hr, start_min, end_hr, end_min
```

## SQL Query:
```sql
SELECT sum(credits) , course_dept_name FROM department GROUP BY dept_name
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