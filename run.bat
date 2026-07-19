
cd C:\RoadSeva
D:\Anaconda\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload --env-file .env

#test cases check everything is working fine
D:\Anaconda\python.exe -m pytest tests/ -v

#for every files created or edited or saved in the project, run the following commands to update the github repository  
git add .
git commit -m "update"
git push origin main

This shows all files recursively, excluding the .git folder noise
dir C:\RoadSeva /b /a-d
dir C:\RoadSeva /b /s | findstr /v ".git"

#http://localhost:8000
#http://127.0.0.1:8000

delete all reports from the database (for testing purposes only)
D:\Anaconda\python.exe -c "from dotenv import load_dotenv; load_dotenv(); import database; conn=database.get_conn(); conn.execute('DELETE FROM reports'); conn.commit(); conn.close(); print('All reports deleted')"
output: All reports deleted


#@echo off
cd C:\RoadSeva
D:\Anaconda\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

#recover password for admin and field engineer
cd C:\RoadSeva
D:\Anaconda\python.exe recover.py

#to delete the database and start fresh from admin login
#cd C:\RoadSeva
#del roadseva.db
#D:\Anaconda\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
#D:\Anaconda\python.exe create_demo_accounts.py

#to look for demo data and test the AI analysis
#cd C:\RoadSeva
#D:\Anaconda\python.exe demo_data.py

@echo off
call conda activate base
cd C:\RoadSeva
python -m uvicorn main:app --reload
pause


Clean the test data before the meeting:

cd C:\RoadSeva
D:\Anaconda\python.exe -c "import sqlite3; c=sqlite3.connect('roadseva.db'); c.execute('DELETE FROM reports'); c.execute('DELETE FROM sessions'); c.commit(); print('Clean')"

Accounts — What Happens After Delete
D:\Anaconda\python.exe check.py
