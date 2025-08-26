import os
import json
import time
import asyncio
import re
from typing import Union
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from twocaptcha import TwoCaptcha
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = FastAPI()

# Configuration from environment variables
TWOCAPTCHA_API_KEY = os.getenv('TWOCAPTCHA_API_KEY')
CLIENT_ID = os.getenv('WELLNESSLIVING_CLIENT_ID')
CLIENT_SECRET = os.getenv('WELLNESSLIVING_CLIENT_SECRET')
TOKEN_URL = os.getenv('TOKEN_URL', 'https://access.uat-api.wellnessliving.io/oauth2/token')

# Validate required environment variables
required_vars = ['TWOCAPTCHA_API_KEY', 'WELLNESSLIVING_CLIENT_ID', 'WELLNESSLIVING_CLIENT_SECRET']
for var in required_vars:
    if not os.getenv(var):
        raise ValueError(f'Missing required environment variable: {var}')

class TokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str

class ErrorResponse(BaseModel):
    error: str
    message: str
    status_code: int

def solve_cloudflare_with_2captcha():
    """
    Use Selenium with 2Captcha to bypass Cloudflare protection
    """
    solver = TwoCaptcha(TWOCAPTCHA_API_KEY)
    
    chrome_options = Options()
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
    
    driver = webdriver.Chrome(options=chrome_options)
    
    try:
        print("Loading the page...")
        driver.get(TOKEN_URL)
        time.sleep(5)
        
        # Check if there's a Cloudflare challenge
        if "Just a moment" in driver.page_source or "cf-challenge" in driver.page_source:
            print("Cloudflare challenge detected. Solving with 2Captcha...")
            
            try:
                # Wait for the challenge frame to appear
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"))
                )
                
                iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")
                iframe_src = iframe.get_attribute('src')
                
                # Extract site key from iframe src
                site_key_match = re.search(r'sitekey=([^&]+)', iframe_src)
                if site_key_match:
                    site_key = site_key_match.group(1)
                    print(f"Found site key: {site_key}")
                    
                    # Solve Turnstile captcha with 2Captcha
                    result = solver.turnstile(
                        sitekey=site_key,
                        url=driver.current_url
                    )
                    
                    print(f"Captcha solved: {result['code']}")
                    
                    # Inject the solution into the page
                    driver.execute_script(f"""
                        document.querySelector('input[name="cf-turnstile-response"]').value = '{result['code']}';
                        document.querySelector('form').submit();
                    """)
                    
                    time.sleep(10)
                    
            except Exception as e:
                print(f"Error solving captcha: {e}")
                return None
        
        # Now make the OAuth request using JavaScript
        script = f"""
        return fetch('{TOKEN_URL}', {{
            method: 'POST',
            headers: {{
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json'
            }},
            body: 'client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&grant_type=client_credentials'
        }}).then(response => response.text());
        """
        
        result = driver.execute_script(script)
        return result
        
    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        driver.quit()

def fallback_request():
    """
    Fallback method using requests with proper headers
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept-Language': 'en-US,en;q=0.9',
        'Origin': 'https://access.uat-api.wellnessliving.io',
        'Referer': 'https://access.uat-api.wellnessliving.io/',
    }
    
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'client_credentials'
    }
    
    try:
        response = requests.post(TOKEN_URL, headers=headers, data=data)
        return response
    except Exception as e:
        print(f"Fallback request failed: {e}")
        return None

async def async_solve_cloudflare():
    """
    Async wrapper for the 2Captcha solving process
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, solve_cloudflare_with_2captcha)

@app.get("/get-token", response_model=Union[TokenResponse, ErrorResponse])
async def get_token():
    """
    Get OAuth token, handling Cloudflare challenges with 2Captcha
    """
    
    # First try the simple request
    print("Trying simple request first...")
    response = fallback_request()
    
    if response:
        print(f"Response status: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        print(f"Response text (first 500 chars): {response.text[:500]}")
        
        if response.status_code == 200:
            try:
                token_data = response.json()
                print(f"Successfully parsed JSON: {token_data}")
                return TokenResponse(**token_data)
            except Exception as e:
                print(f"Failed to parse JSON: {e}")
                print(f"Raw response: {response.text}")
        
        # Check if we got Cloudflare challenge
        if "Just a moment" in response.text or "cf-challenge" in response.text:
            print("Cloudflare challenge detected, using 2Captcha...")
            use_2captcha = True
        else:
            # Return detailed error for non-200 responses
            return ErrorResponse(
                error="http_error",
                message=f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code
            )
    else:
        print("No response received from fallback_request - DNS issue detected, going straight to 2Captcha...")
        use_2captcha = True
    
    # Use 2Captcha method
    if 'use_2captcha' in locals() and use_2captcha:
        try:
            # Use async wrapper with timeout
            result = await asyncio.wait_for(async_solve_cloudflare(), timeout=300)  # 5 minute timeout
            if result:
                print(f"2Captcha result: {result}")
                token_data = json.loads(result)
                return TokenResponse(**token_data)
            else:
                return ErrorResponse(
                    error="captcha_failed",
                    message="2Captcha method returned no result",
                    status_code=500
                )
        except asyncio.TimeoutError:
            print("2Captcha method timed out")
            return ErrorResponse(
                error="timeout",
                message="Captcha solving timed out after 5 minutes",
                status_code=408
            )
        except Exception as e:
            print(f"2Captcha method failed: {e}")
            return ErrorResponse(
                error="captcha_failed",
                message=f"Captcha solving failed: {str(e)}",
                status_code=500
            )
    
    # Fallback error
    return ErrorResponse(
        error="all_methods_failed",
        message="Both simple request and 2Captcha method failed",
        status_code=500
    )
