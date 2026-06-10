import os
import sys
import requests
from bs4 import BeautifulSoup
import time
from dotenv import load_dotenv

# Load environment variables from .env file (for local testing)
load_dotenv()

USERNAME = os.environ.get('PA_USERNAME')
PASSWORD = os.environ.get('PA_PASSWORD')

if not USERNAME or not PASSWORD:
    print("❌ Error: PA_USERNAME and PA_PASSWORD must be set")
    sys.exit(1)

LOGIN_URL = "https://www.pythonanywhere.com/login/"
DASHBOARD_URL = f"https://www.pythonanywhere.com/user/{USERNAME}/webapps/"

def renew():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    
    try:
        # 1. Get login page
        print(f"🔐 Logging in as {USERNAME}...")
        login_page = session.get(LOGIN_URL, timeout=10)
        login_page.raise_for_status()
        
        soup = BeautifulSoup(login_page.content, 'html.parser')
        csrf_token = soup.find('input', {'name': 'csrfmiddlewaretoken'})
        
        if not csrf_token:
            print("❌ Could not find CSRF token on login page")
            return False
        
        csrf_token = csrf_token['value']
        
        # 2. Submit login
        payload = {
            'csrfmiddlewaretoken': csrf_token,
            'auth-username': USERNAME,
            'auth-password': PASSWORD,
            'login_view-current_step': 'auth'
        }
        
        response = session.post(
            LOGIN_URL, 
            data=payload, 
            headers={'Referer': LOGIN_URL},
            timeout=10,
            allow_redirects=True
        )
        response.raise_for_status()
        
        # Check multiple indicators of successful login
        if "Log out" not in response.text and "logout" not in response.text.lower():
            print("❌ Login failed - 'Log out' not found in response")
            print(f"Response URL: {response.url}")
            return False
            
        if "login" in response.url.lower():
            print("❌ Login failed - still on login page")
            return False
        
        print("✅ Login successful")
        
        # 3. Access dashboard
        print("📊 Checking dashboard...")
        time.sleep(1)  # Be polite to the server
        
        dashboard = session.get(DASHBOARD_URL, timeout=10)
        dashboard.raise_for_status()
        soup = BeautifulSoup(dashboard.content, 'html.parser')
        
        # 4. Find extend button/form
        forms = soup.find_all('form', action=True)
        extend_action = None
        
        for form in forms:
            action = form.get('action', '')
            if "/extend" in action.lower():
                extend_action = action
                print(f"🔍 Found extend action: {action}")
                break
        
        if not extend_action:
            print("ℹ️  No extend button found.")
            print("   This usually means your app doesn't need renewal yet.")
            return True  # Not an error - just nothing to extend
        
        # 5. Get CSRF token from dashboard
        dashboard_csrf = soup.find('input', {'name': 'csrfmiddlewaretoken'})
        if not dashboard_csrf:
            print("❌ Could not find CSRF token on dashboard")
            return False
        
        # 6. Submit extend request
        extend_url = f"https://www.pythonanywhere.com{extend_action}"
        print(f"⏰ Extending web app at {extend_url}...")
        
        result = session.post(
            extend_url,
            data={'csrfmiddlewaretoken': dashboard_csrf['value']},
            headers={'Referer': DASHBOARD_URL},
            timeout=10
        )
        result.raise_for_status()
        
        # Verify extension was successful
        if result.status_code == 200:
            # Check if we're back on the dashboard
            if "webapps" in result.url.lower():
                print("✅ Web app extended successfully!")
                return True
            else:
                print(f"⚠️  Unexpected redirect to: {result.url}")
                return False
        else:
            print(f"❌ Extension failed with status: {result.status_code}")
            return False
            
    except requests.Timeout:
        print("❌ Request timed out")
        return False
    except requests.RequestException as e:
        print(f"❌ Network error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False

if __name__ == "__main__":
    success = renew()
    sys.exit(0 if success else 1)
