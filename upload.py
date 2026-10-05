import urllib.request
import re
import requests

def upload_imgbb(filepath):
    # Get auth token
    req = urllib.request.Request('https://imgbb.com/')
    html = urllib.request.urlopen(req).read().decode('utf-8')
    token_match = re.search(r'auth_token="([^"]+)"', html)
    if not token_match:
        print('Could not find auth token')
        return None
    auth_token = token_match.group(1)
    
    url = 'https://imgbb.com/json'
    files = {'source': open(filepath, 'rb')}
    data = {
        'type': 'file',
        'action': 'upload',
        'timestamp': '',
        'auth_token': auth_token,
        'nsfw': '0'
    }
    res = requests.post(url, files=files, data=data)
    try:
        return res.json()['image']['url']
    except Exception as e:
        print('Error:', e, res.text)
        return None

print('Sayem:', upload_imgbb('d:/Upload/media/avatar-sayem.jpg'))
print('Shajeda:', upload_imgbb('d:/Upload/media/avatar-shajeda.webp'))
