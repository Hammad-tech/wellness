import json
import time
import asyncio
import re
import os
import tempfile
from typing import Union, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from twocaptcha import TwoCaptcha
from dotenv import load_dotenv

# --------- Config ---------
load_dotenv()

TWOCAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
CLIENT_ID = os.getenv("WELLNESSLIVING_CLIENT_ID")
CLIENT_SECRET = os.getenv("WELLNESSLIVING_CLIENT_SECRET")
TOKEN_URL = os.getenv("TOKEN_URL", "https://access.uat-api.wellnessliving.io/oauth2/token")
CF_BYPASS_HEADER_VALUE = os.getenv("CF_BYPASS_HEADER_VALUE")  # e.g., MorningLightStudio-HJutxWaYn5-flag

for k in ["TWOCAPTCHA_API_KEY", "WELLNESSLIVING_CLIENT_ID", "WELLNESSLIVING_CLIENT_SECRET"]:
    if not os.getenv(k):
        raise ValueError(f"Missing required env var: {k}")

app = FastAPI()

class TokenResponse(BaseModel):
    access_token: str
    expires_in: int
    token_type: str

# ---------- HTTP helpers ----------

def fallback_request(timeout=20) -> requests.Response:
    """
    Try the plain OAuth POST with realistic headers and optional Cloudflare bypass header.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TokenFetcher/1.0; Render)",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://access.uat-api.wellnessliving.io",
        "Referer": "https://access.uat-api.wellnessliving.io/",
    }
    if CF_BYPASS_HEADER_VALUE:
        headers["x-firewall-rule"] = CF_BYPASS_HEADER_VALUE

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    return requests.post(TOKEN_URL, headers=headers, data=data, timeout=timeout)

def new_chrome_driver() -> webdriver.Chrome:
    """
    Start a fresh, isolated headless Chrome so user-data-dir is never locked.
    """
    tmp_profile = tempfile.mkdtemp(prefix="chrome-")
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")
    chrome_options.add_argument(f"--user-data-dir={tmp_profile}")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    )
    return webdriver.Chrome(options=chrome_options)

def solve_cloudflare_with_2captcha() -> Union[str, None]:
    """
    Use Selenium + 2Captcha (Turnstile) to pass CF, then POST OAuth via fetch.
    Returns raw text from fetch or None.
    """
    solver = TwoCaptcha(TWOCAPTCHA_API_KEY)
    driver = new_chrome_driver()

    try:
        driver.get(TOKEN_URL)
        time.sleep(4)

        page = driver.page_source or ""
        if "Just a moment" in page or "challenges.cloudflare.com" in page or "cf-challenge" in page:
            # Optional: attempt to pick up sitekey from the Turnstile iframe
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"))
                )
                iframe = driver.find_element(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")
                iframe_src = iframe.get_attribute("src") or ""
                m = re.search(r"sitekey=([^&]+)", iframe_src)
                if m:
                    site_key = m.group(1)
                    result = solver.turnstile(sitekey=site_key, url=driver.current_url)
                    token_code = result.get("code")

                    # Best-effort set token; many CF flows accept token auto-submitted by Turnstile widget
                    driver.execute_script("""\
                      const el = document.querySelector('input[name="cf-turnstile-response"]');
                      if (el) { el.value = arguments[0]; }
                    """, token_code)
                    time.sleep(1)
            except Exception as e:
                print(f"[2Captcha] Challenge solve step skipped/failed: {e}")

        # Finally: perform the OAuth POST via browser fetch (benefits from CF cookies/session)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if CF_BYPASS_HEADER_VALUE:
            # Often not required when running inside browser, but harmless
            headers["x-firewall-rule"] = CF_BYPASS_HEADER_VALUE

        js = f"""
        return fetch("{TOKEN_URL}", {{
            method: "POST",
            headers: {json.dumps(headers)},
            body: "client_id={CLIENT_ID}&client_secret={CLIENT_SECRET}&grant_type=client_credentials"
        }}).then(r => r.text());
        """
        return driver.execute_script(js)
    except Exception as e:
        print(f"[2Captcha Selenium] Error: {e}")
        return None
    finally:
        driver.quit()

async def run_2captcha_with_timeout(timeout_sec=300) -> Optional[str]:
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, solve_cloudflare_with_2captcha), timeout=timeout_sec)
    except asyncio.TimeoutError:
        print("[2Captcha Selenium] Timeout")
        return None

class ErrorResponse(BaseModel):
    error: str
    message: str
    status_code: int = 500

@app.get("/get-token", response_model=Union[TokenResponse, ErrorResponse])
async def get_token():
    """
    Get OAuth token, handling Cloudflare challenges with 2Captcha
    """
    # First try the simple request
    print("Trying simple request first...")
    try:
        response = fallback_request()
    except Exception as e:
        print(f"Fallback request failed: {e}")
        response = None
    
    if response and response.status_code == 200:
        try:
            token_data = response.json()
            print(f"Successfully parsed JSON: {token_data}")
            return TokenResponse(**token_data)
        except Exception as e:
            print(f"Failed to parse JSON: {e}")
            print(f"Raw response: {response.text}")
    
    # Check if we should try 2Captcha
    should_use_2captcha = False
    if response is None:
        print("No response from fallback request, trying 2Captcha...")
        should_use_2captcha = True
    elif "Just a moment" in response.text or "cf-challenge" in response.text:
        print("Cloudflare challenge detected, using 2Captcha...")
        should_use_2captcha = True
    
    if should_use_2captcha:
        try:
            result = await run_2captcha_with_timeout(timeout_sec=300)
            if result:
                try:
                    token_data = json.loads(result)
                    return TokenResponse(**token_data)
                except json.JSONDecodeError as e:
                    print(f"Failed to parse 2Captcha result: {e}")
                    return ErrorResponse(
                        error="invalid_response",
                        message="Received invalid JSON from 2Captcha solution",
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
    
    # If we get here, all methods failed
    return ErrorResponse(
        error="all_methods_failed",
        message="All methods to obtain token failed",
        status_code=500
    )
