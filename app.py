from flask import Flask, request, jsonify, render_template, redirect, url_for
from openai import OpenAI
import os
import docx
import sqlite3
import requests
import json
from werkzeug.utils import secure_filename
from pathlib import Path
from pdf2image import convert_from_path, convert_from_bytes
from PIL import Image
import pytesseract
import pdfplumber
import tempfile
from io import BytesIO
import datetime
from decimal import Decimal
from re import sub
import traceback
from dotenv import load_dotenv

load_dotenv()

apikey=  os.getenv("API_KEY", "")
app = Flask(__name__)
ordway_app_url = os.getenv("ORDWAY_APP_URL", "https://staging.ordwaylabs.com/")
cust_api_url =  ordway_app_url + "api/v1/customers"
sub_api_url = ordway_app_url + "api/v1/subscriptions"
headers = {
        "Content-Type": os.getenv("CONTENT_TYPE", "application/json"),
        "X-User-Company": os.getenv("X_USER_COMPANY", ""),
        "X-User-Token": os.getenv("X_USER_TOKEN", ""),
        "X-User-Email": os.getenv("X_USER_EMAIL", ""),
        "X-Company-Token": os.getenv("X_COMPANY_TOKEN", "")
    }
 


client = OpenAI(api_key = apikey)
app.config['UPLOAD_FOLDER'] = tempfile.gettempdir()

# 🔥 NEW: OCR + PDF to Text
def extract_text_from_file(file):
    filename = file.filename.lower()
    if filename.endswith('.pdf'):
        images = convert_from_bytes(file.read())
        text = ""
        for image in images:
            text += pytesseract.image_to_string(image)
        return text
    elif filename.endswith(('.png', '.jpg', '.jpeg')):
        image = Image.open(file)
        return pytesseract.image_to_string(image)
    else:
        return None

# GPT Prompt
def build_prompt(contract_text: str):
    return f"""
You are a contract analysis AI. Extract the following metadata from the contract provided below:

1. Contract Type
2. Start Date
3. End Date
4. Renewal Terms
5. Counter parties
6. Governing Law
7. Payment Terms
8. Termination Clause Summary
9. Auto-renewal (Yes/No)
10. Subscription Fee
11. Products Purchased

Provide the result in the following JSON format:

{{
  "contract_type": "",
  "start_date": "",
  "end_date": "",
  "renewal_terms": "",
  "parties": [],
  "governing_law": "",
  "payment_terms": "",
  "termination_summary": "",
  "auto_renewal": "",
  "subscription_fee": "",
  "products_purchased": []
}}

Contract Text:
\"\"\"
{contract_text}
\"\"\"
"""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/extract', methods=['POST'])
def extract_metadata():
    contract_text = request.form.get('contract_text')
    contract_file = request.files.get('contract_file')

    # 🔥 NEW: If file is uploaded, use OCR
    if contract_file:
        contract_text = extract_text_from_file(contract_file)

    if not contract_text:
        return jsonify({'error': 'No contract text or valid file provided'}), 400

    prompt = build_prompt(contract_text)

    try:
        # Try GPT-4 first
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": "You are a helpful AI assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0
            )
        except Exception as e:
            print("GPT-4 failed, falling back to GPT-3.5:", e)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a helpful AI assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0
            )
        metadata = response.choices[0].message.content
        #print("metadata:", metadata)
        metadata_json = jsonify({'metadata': metadata})
        #print("metadata_json:", metadata_json)
        print("extracted metadata:", metadata)
        # Send metadata to subscription API
        api_status, api_response = create_ordway_subscription(metadata)
        #print("API Response:", api_status, api_response)
        return redirect(ordway_app_url+"subscriptions/"+json.loads(api_response).get("id"))

        #return metadata_json
    except Exception as e:
        print("API error:",traceback.format_exc())
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)


@app.route('/extract', methods=['POST'])
def extract_text_from_file(file):
    filename = file.filename.lower()
    file_data = file.read()

    # Try machine-readable PDF
    if filename.endswith('.pdf'):
        try:
            with pdfplumber.open(BytesIO(file_data)) as pdf:
                text = '\n'.join(page.extract_text() or "" for page in pdf.pages).strip()
            if text:
                return text  # ✅ Use this if text is found
        except Exception as e:
            print("pdfplumber failed, falling back to OCR:", e)

        # 🔁 Fallback to OCR if no text found
        images = convert_from_bytes(file_data)
        text = ''
        for image in images:
            text += pytesseract.image_to_string(image)
        return text

    elif filename.endswith(('.png', '.jpg', '.jpeg')):
        image = Image.open(BytesIO(file_data))
        return pytesseract.image_to_string(image)

    return None

# Example function to send metadata to external subscription API
def create_ordway_customer(metadata_json):
    try:
        print(metadata_json)
        #check if customer already exists
        cust_name = json.loads(metadata_json).get("parties", ["Unknown"])[1]
        print("Customer Name:", cust_name + "----" + cust_api_url+"?name="+"'"+cust_name+"'")

        params = {
            "name": cust_name
        }
        
        response = requests.get(cust_api_url, params=params, headers=headers)
        print("Customer API Response:", response.status_code, response.text)
        if response.status_code == 200 and "id" in response.text:
            print("Customer already exists, use this customer id.")
            cust_id = json.loads(response.text)[0].get("id")
            print("Customer ID:", cust_id)
            #return response
        else:
            print("Customer not found, creating new customer.")
            response = requests.post(cust_api_url, headers=headers, json=create_customer_payload(metadata_json))
            print("Customer API Response:", response.status_code, response.text)
            if response.status_code == 200 and "id" in response.text:
                cust_id = json.loads(response.text).get("id")
        
        print("Customer ID:", cust_id)
        return cust_id
    except Exception as e:
        print("API POST error:", traceback.format_exc())
        return 500, str(e)
    
def create_ordway_subscription(metadata_json):
    try:
        print(metadata_json)
        # Create customer first
        cust_id = create_ordway_customer(metadata_json)
        print("Customer Id found or created:", cust_id)
        if cust_id == None:
            raise Exception("Customer does not exist or cannot be created")
        
        print("Customer ID:", cust_id)
        response = requests.post(sub_api_url, headers=headers, json=create_subscription_payload(metadata_json,cust_id))
        print("Subscription API Response:", response.status_code, response.text)
        return response.status_code, response.text
    except Exception as e:
        print("API POST error:", traceback.format_exc())
        return 500, str(e)

def create_customer_payload(metadata):
    try:
        metadata_dict = json.loads(metadata)
        payload = {
            #"customer": {
                "name": metadata_dict.get("parties", ["Unknown"])[1],
                #"metadata": metadata_dict
            #}
        }
        print("Payload:", payload)
        return payload
    except json.JSONDecodeError as e:
        print("JSON decode error:", traceback.format_exc())
        return None
    
def create_subscription_payload(metadata, cust_id):
    try:
        metadata_dict = json.loads(metadata)
        print("fee: ", str(Decimal(sub(r'[^\d.]', '', metadata_dict.get("subscription_fee")))))
        
        #datetime.datetime.strptime(metadata_dict.get("start_date"), "%m/%d/%Y").strftime("%Y-%m-%d")

        payload = {
                "customer_id": cust_id,
                "billing_start_date": normalize_date(metadata_dict.get("start_date")),
                "service_start_date": normalize_date(metadata_dict.get("start_date")),
                "contract_effective_date": normalize_date(metadata_dict.get("start_date")),
                "contract_end_date": normalize_date(metadata_dict.get("end_date")),
                "contract_type": metadata_dict.get("contract_type"),
                "auto_renew": metadata_dict.get("auto_renewal"),
                #"payment_terms": metadata_dict.get("payment_terms"),
                "plans": [
                    {
                        "product_id": "P-00003",
                        "charge_name": metadata_dict.get("products_purchased", ["Unknown"])[0],
                        "quantity": 1,
                        "pricing_model": "per unit",
                        "effective_price": str(Decimal(sub(r'[^\d.]', '', metadata_dict.get("subscription_fee")))),
                        "billing_period": "Annually",
                        "charge_type": "Recurring",
                        "plan_id": "PLN-00002",
                        "list_price": str(Decimal(sub(r'[^\d.]', '', metadata_dict.get("subscription_fee")))) 

                    }
                ],

        }
        print("Payload:", payload)
        return payload
    except json.JSONDecodeError as e:
        print("JSON decode error:", traceback.format_exc())
        return None
    
def normalize_date(date_str):
# List of common date formats
    formats = [
        "%B %d, %Y",    # January 1, 2024
        "%d %B %Y",     # 1 January 2024
        "%m/%d/%Y",     # 01/01/2024
        "%d-%m-%Y",     # 01-01-2024
        "%Y-%m-%d",     # 2024-01-01
        "%d.%m.%Y",     # 01.01.2024
        "%b %d, %Y",    # Jan 1, 2024
        "%d %b %Y"      # 1 Jan 2024
    ]
    
    for fmt in formats:
        try:
            parsed_date = datetime.datetime.strptime(date_str.strip(), fmt)
            return parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Date format for '{date_str}' not recognized.")
    return None  # or raise an error if needed
