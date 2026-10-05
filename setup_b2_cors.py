import os
import boto3
from botocore.config import Config

b2 = boto3.client(
    's3',
    endpoint_url='https://s3.us-east-005.backblazeb2.com',
    aws_access_key_id='005bb11aa1b24790000000001',
    aws_secret_access_key='K005yKDgsVjAjDVYpAhnOrL8KPWfuCQ',
    config=Config(signature_version='s3v4')
)

cors_configuration = {
    'CORSRules': [{
        'AllowedHeaders': ['*'],
        'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
        'AllowedOrigins': ['https://moo.qzz.io', 'http://localhost:3000', 'http://localhost:4000', 'http://localhost:8000', 'http://127.0.0.1:8000'],
        'ExposeHeaders': ['ETag', 'x-amz-server-side-encryption'],
        'MaxAgeSeconds': 3600
    }]
}

try:
    b2.put_bucket_cors(
        Bucket='shajeda',
        CORSConfiguration=cors_configuration
    )
    print("SUCCESS: CORS rules have been applied to the 'shajeda' bucket!")
except Exception as e:
    print(f"FAILED to set CORS: {e}")
