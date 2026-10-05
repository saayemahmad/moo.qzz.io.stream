with open('backend/main.py', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if '@app.get("/uploads")' in line:
        print(''.join(lines[i:i+35]))
