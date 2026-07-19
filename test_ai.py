import os
from dotenv import load_dotenv
load_dotenv()
import glob

files = glob.glob('uploads/*')
print('Files in uploads folder:', files)

if files:
    from severity import analyse_severity
    result = analyse_severity(files[0], 'Pothole')
    print('AI Result:', result)
else:
    print('uploads folder is empty — photo not saved to disk')