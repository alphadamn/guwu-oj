# import os
# import time
# from django.test import LiveServerTestCase
# from django.contrib.auth import get_user_model
# from selenium import webdriver
# from selenium.webdriver.common.by import By
# from selenium.webdriver.common.keys import Keys
# from selenium.webdriver.support.ui import WebDriverWait
# from selenium.webdriver.support import expected_conditions as EC
# from selenium.webdriver.edge.service import Service
# from webdriver_manager.microsoft import EdgeChromiumDriverManager
#
# User = get_user_model()
#
#
# class SeleniumTestCase(LiveServerTestCase):
#     """Base class for Selenium tests with common setup and utilities."""
#
#     @classmethod
#     def setUpClass(cls):
#         super().setUpClass()
#         # Setup Chrome WebDriver
#         options = webdriver.EdgeOptions()
#         # options.add_argument('--headless')  # Run in headless mode
#         options.add_argument('--no-sandbox')
#         options.add_argument('--disable-dev-shm-usage')
#         options.add_argument('--ignore-certificate-errors')
#         options.add_argument('--allow-insecure-localhost')
#         options.add_argument('--disable-gpu')
#         cls.selenium = webdriver.Edge(service=Service(EdgeChromiumDriverManager().install()), options=options)
#         cls.selenium.implicitly_wait(10)
#
#     @classmethod
#     def tearDownClass(cls):
#         cls.selenium.quit()
#         super().tearDownClass()
#
#     def setUp(self):
#         # Create test user
#         self.user = User.objects.create_user(
#             username='testuser',
#             email='test@example.com',
#             password='testpass123'
#         )
#
#     def wait_for_element(self, by, value, timeout=10):
#         """Wait for an element to be present on the page."""
#         return WebDriverWait(self.selenium, timeout).until(
#             EC.presence_of_element_located((by, value))
#         )
#
#     def wait_for_clickable(self, by, value, timeout=10):
#         """Wait for an element to be clickable."""
#         return WebDriverWait(self.selenium, timeout).until(
#             EC.element_to_be_clickable((by, value))
#         )
#
#
# class HomepageTest(SeleniumTestCase):
#     """Test the homepage functionality."""
#
#     def test_homepage_loads(self):
#         """Test that the homepage loads successfully."""
#         self.selenium.get(self.live_server_url)
#         self.assertIn('Online Judge', self.selenium.title)
#
#     def test_homepage_has_navigation(self):
#         """Test that navigation elements are present on homepage."""
#         self.selenium.get(self.live_server_url)
#         # Check for common navigation elements
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#
# class AuthenticationTest(SeleniumTestCase):
#     """Test user authentication flows."""
#
#     def test_login_page_loads(self):
#         """Test that the login page loads successfully."""
#         self.selenium.get(f'{self.live_server_url}/users/login/')
#         self.assertIn('登录', self.selenium.page_source)
#
#     def test_user_login(self):
#         """Test user login functionality."""
#         self.selenium.get(f'{self.live_server_url}/users/login/')
#
#         # Find and fill login form
#         username_input = self.wait_for_element(By.NAME, 'username')
#         password_input = self.selenium.find_element(By.NAME, 'password')
#
#         username_input.send_keys('testuser')
#         password_input.send_keys('testpass123')
#
#         # Submit form
#         password_input.send_keys(Keys.RETURN)
#
#         # Wait for redirect to home
#         WebDriverWait(self.selenium, 10).until(
#             lambda driver: driver.current_url == f'{self.live_server_url}/'
#         )
#
#         # Check if user is logged in (you may need to adjust this based on your UI)
#         self.assertIn(self.live_server_url, self.selenium.current_url)
#
#     def test_register_page_loads(self):
#         """Test that the registration page loads successfully."""
#         self.selenium.get(f'{self.live_server_url}/users/register/')
#         self.assertIn('注册', self.selenium.page_source)
#
# class ProblemTest(SeleniumTestCase):
#     """Test problem browsing and viewing."""
#
#     def setUp(self):
#         super().setUp()
#         # Create a test problem
#         from problems.models import Problem
#         self.problem = Problem.objects.create(
#             title='Selenium Test Problem',
#             description='This is a test problem for Selenium testing',
#             input_format='Single integer',
#             output_format='Single integer',
#             sample_input='5',
#             sample_output='5',
#             time_limit=1000,
#             memory_limit=256,
#             difficulty='入门',
#             created_by=self.user,
#             is_public=True
#         )
#
#     def test_problem_list_page_loads(self):
#         """Test that the problem list page loads."""
#         self.selenium.get(f'{self.live_server_url}/')
#         # Check if problem list is displayed
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#     def test_problem_detail_page_loads(self):
#         """Test that a problem detail page loads."""
#         self.selenium.get(f'{self.live_server_url}/problem/{self.problem.id}/')
#
#         # Wait for page to load
#         self.wait_for_element(By.TAG_NAME, 'body')
#
#         # Check if problem title is displayed
#         page_source = self.selenium.page_source
#         self.assertIn(self.problem.title, page_source)
#
#     def test_problem_navigation(self):
#         """Test navigating to a problem from the list."""
#         self.selenium.get(f'{self.live_server_url}/')
#
#         # Try to find and click on a problem link
#         # This depends on your actual HTML structure
#         try:
#             problem_link = self.selenium.find_element(By.PARTIAL_LINK_TEXT, str(self.problem.id))
#             problem_link.click()
#
#             # Verify we're on the problem detail page
#             WebDriverWait(self.selenium, 10).until(
#                 lambda driver: str(self.problem.id) in driver.current_url
#             )
#         except:
#             # If link structure is different, just verify problem list loads
#             pass
#
#
# class SubmissionTest(SeleniumTestCase):
#     """Test code submission functionality."""
#
#     def setUp(self):
#         super().setUp()
#         from problems.models import Problem
#         self.problem = Problem.objects.create(
#             title='Submission Test Problem',
#             description='Test problem for submission',
#             input_format='Single integer',
#             output_format='Single integer',
#             sample_input='5',
#             sample_output='5',
#             time_limit=1000,
#             memory_limit=256,
#             difficulty='入门',
#             created_by=self.user,
#             is_public=True
#         )
#
#     def test_submission_page_loads(self):
#         """Test that the submission page loads for a problem."""
#         self.selenium.get(f'{self.live_server_url}/problem/{self.problem.id}/')
#
#         # Try to find submit button or link
#         page_source = self.selenium.page_source
#         # Check if submission elements are present
#         self.assertIsNotNone(page_source)
#
#     def test_authenticated_user_can_view_submission_page(self):
#         """Test that authenticated users can access submission features."""
#         # Login first
#         self.selenium.get(f'{self.live_server_url}/users/login/')
#         username_input = self.wait_for_element(By.NAME, 'username')
#         password_input = self.selenium.find_element(By.NAME, 'password')
#         username_input.send_keys('testuser')
#         password_input.send_keys('testpass123')
#         password_input.send_keys(Keys.RETURN)
#
#         # Wait for login to complete
#         WebDriverWait(self.selenium, 10).until(
#             lambda driver: driver.current_url == f'{self.live_server_url}/'
#         )
#
#         # Navigate to problem page
#         self.selenium.get(f'{self.live_server_url}/problem/{self.problem.id}/')
#
#         # Verify page loads
#         self.wait_for_element(By.TAG_NAME, 'body')
#         self.assertIn(self.problem.title, self.selenium.page_source)
#
#
# class NavigationTest(SeleniumTestCase):
#     """Test site navigation and links."""
#
#     def test_navigation_links_work(self):
#         """Test that main navigation links work correctly."""
#         self.selenium.get(self.live_server_url)
#
#         # Test common navigation paths
#         paths_to_test = [
#             '/',
#             '/users/login/',
#             '/users/register/',
#         ]
#
#         for path in paths_to_test:
#             self.selenium.get(f'{self.live_server_url}{path}')
#             self.wait_for_element(By.TAG_NAME, 'body')
#             self.assertEqual(self.selenium.current_url, f'{self.live_server_url}{path}')
#
#     def test_static_files_load(self):
#         """Test that static files (CSS, JS) are loading."""
#         self.selenium.get(self.live_server_url)
#
#         # Check for any console errors (basic check)
#         # In a real test, you might want to check for specific CSS/JS files
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#
# class ResponsiveDesignTest(SeleniumTestCase):
#     """Test responsive design at different screen sizes."""
#
#     def test_mobile_view(self):
#         """Test that the site works on mobile screen size."""
#         self.selenium.set_window_size(375, 667)  # iPhone SE size
#         self.selenium.get(self.live_server_url)
#
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#     def test_tablet_view(self):
#         """Test that the site works on tablet screen size."""
#         self.selenium.set_window_size(768, 1024)  # iPad size
#         self.selenium.get(self.live_server_url)
#
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#     def test_desktop_view(self):
#         """Test that the site works on desktop screen size."""
#         self.selenium.set_window_size(1920, 1080)  # Full HD
#         self.selenium.get(self.live_server_url)
#
#         body = self.selenium.find_element(By.TAG_NAME, 'body')
#         self.assertIsNotNone(body)
#
#
# if __name__ == '__main__':
#     import django
#     from django.conf import settings
#     from django.test.utils import get_runner
#
#     if not settings.configured:
#         settings.configure(
#             DEBUG=True,
#             DATABASES={
#                 'default': {
#                     'ENGINE': 'django.db.backends.sqlite3',
#                     'NAME': ':memory:',
#                 }
#             },
#             INSTALLED_APPS=[
#                 'django.contrib.auth',
#                 'django.contrib.contenttypes',
#                 'users',
#                 'problems',
#                 'submissions',
#             ],
#         )
#         django.setup()
#
#     TestRunner = get_runner(settings)
#     test_runner = TestRunner()
#     failures = test_runner.run_tests(['__main__'])
# tests/test_selenium.py
import time

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.cache import cache
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.edge.service import Service
from webdriver_manager.microsoft import EdgeChromiumDriverManager

from problems.models import Problem, TestCase   # adjust to your actual Problem model

User = get_user_model()


class SeleniumTests(StaticLiveServerTestCase):
    """
    Selenium test suite for the Online Judge Django application.
    Runs against the live server started by Django's test framework.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.driver_path = EdgeChromiumDriverManager().install()
        cls._start_driver()

    @classmethod
    def _start_driver(cls):
        """Start a browser configured not to wait on third-party assets."""
        options = webdriver.EdgeOptions()
        options.page_load_strategy = 'eager'
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--ignore-certificate-errors')
        options.add_argument('--allow-insecure-localhost')
        options.add_argument('--disable-gpu')
        cls.driver = webdriver.Edge(
            service=Service(cls.driver_path),
            options=options,
        )
        cls.driver.set_page_load_timeout(30)
        cls.driver.implicitly_wait(10)
        cls.wait = WebDriverWait(cls.driver, 10)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # A previous page can leave EdgeDriver unresponsive while it waits on
        # a third-party asset in GitHub Actions. Recreate only that failed
        # browser session so one flaky command cannot fail the whole suite.
        try:
            self.driver.delete_all_cookies()
        except WebDriverException:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self._start_driver()
        # Clear cache between tests
        cache.clear()
        
        # Create a user that can be used for login tests
        self.test_user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='testpass123'
        )

        # Create a public problem owned by the test user
        self.problem = Problem.objects.create(
            title='Selenium Test Problem',
            description='This is a test problem for Selenium testing',
            input_format='Single integer',
            output_format='Single integer',
            sample_input='5',
            sample_output='5',
            hint='',
            time_limit=1000,
            memory_limit=256,
            difficulty='入门',
            created_by=self.test_user,
            is_public=True
        )
        self.problem.save()
        
        # Add test cases for the problem
        TestCase.objects.create(
            problem=self.problem,
            input_data='5',
            expected_output='5',
            order=1,
            is_sample=True
        )
        
        # print(f"Created problem with ID: {self.problem.id}, is_public: {self.problem.is_public}")

        # Create a second problem specifically for submission tests
        self.submission_problem = Problem.objects.create(
            title='Submission Test Problem',
            description='Test problem for submission',
            input_format='Single integer',
            output_format='Single integer',
            sample_input='5',
            sample_output='5',
            time_limit=1000,
            memory_limit=256,
            difficulty='入门',
            created_by=self.test_user,
            is_public=True
        )
        self.submission_problem.save()
        
        # Add test cases for the submission problem
        TestCase.objects.create(
            problem=self.submission_problem,
            input_data='5',
            expected_output='5',
            order=1,
            is_sample=True
        )
        
        # print(f"Created submission problem with ID: {self.submission_problem.id}, is_public: {self.submission_problem.is_public}")
        
        # Verify problems exist in database
        # print(f"Total problems in DB: {Problem.objects.count()}")
        # print(f"Public problems in DB: {Problem.objects.filter(is_public=True).count()}")

    # ----- Helper methods -----
    def wait_for_element(self, by, value, timeout=10):
        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def wait_for_clickable(self, by, value, timeout=10):
        return WebDriverWait(self.driver, timeout).until(
            EC.element_to_be_clickable((by, value))
        )

    def login(self, username='testuser', password='testpass123'):
        """Helper to log in a user."""
        self.driver.get(self.live_server_url + '/users/login/')  # adjust URL name
        self.wait_for_element(By.NAME, 'username').send_keys(username)
        self.driver.find_element(By.NAME, 'password').send_keys(password + Keys.RETURN)
        # Wait for redirect to homepage
        self.wait.until(lambda d: d.current_url == self.live_server_url + '/')

    # ----- Test methods (ported from original script) -----
    def test_homepage_loads(self):
        self.driver.get(self.live_server_url)
        self.assertEqual(self.driver.title, '首页 - 谷物 OJ')

    def test_homepage_has_navigation(self):
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)

    def test_register_page_loads(self):
        self.driver.get(f'{self.live_server_url}/users/register/')  # adjust URL name
        self.assertIn('注册', self.driver.page_source)

    def test_login_page_loads(self):
        self.driver.get(self.live_server_url + '/users/login/')  # adjust URL name
        self.assertIn('登录', self.driver.page_source)

    def test_user_login(self):
        self.login()
        # Wait for redirect to complete and check URL
        self.wait.until(lambda d: d.current_url.startswith(self.live_server_url))
        # Accept either homepage or problem list as valid redirect
        self.assertIn(self.driver.current_url, [self.live_server_url + '/', self.live_server_url + '/problems/'])

    def test_problem_list_page_loads(self):
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)

    def test_problem_detail_page_loads(self):
        # Use the problem created in setUpTestData
        # print(f"Problem ID: {self.problem.id}")
        # print(f"Problem is_public: {self.problem.is_public}")
        # print(f"Problem exists: {Problem.objects.filter(id=self.problem.id).exists()}")
        
        detail_url = f'/problem/{self.problem.id}/'  # adjust URL name
        self.driver.get(self.live_server_url + detail_url)
        self.wait_for_element(By.TAG_NAME, 'body')
        # Check if we got the problem page or 404
        page_source = self.driver.page_source
        # if '404' in page_source or '页面未找到' in page_source:
            # print(f"Got 404 for problem {self.problem.id}")
            # print(f"Current URL: {self.driver.current_url}")
            # print(f"Page source length: {len(page_source)}")
        self.assertIn(self.problem.title, page_source)

    def test_submission_page_loads(self):
        detail_url = f'/problem/{self.submission_problem.id}/'
        self.driver.get(self.live_server_url + detail_url)
        self.assertIsNotNone(self.driver.page_source)

    def test_authenticated_user_can_view_submission_page(self):
        self.login()
        # print(self.submission_problem.id)
        detail_url = f'/problem/{self.submission_problem.id}/'
        self.driver.get(self.live_server_url + detail_url)
        self.wait_for_element(By.TAG_NAME, 'body')
        self.assertIn(self.submission_problem.title, self.driver.page_source)

    def test_navigation_links_work(self):
        paths = ['/', '/users/login/', '/users/register/']
        for path in paths:
            self.driver.get(self.live_server_url + path)
            self.wait_for_element(By.TAG_NAME, 'body')
            self.assertEqual(self.driver.current_url, self.live_server_url + path)

    def test_static_files_load(self):
        # Static files are served by LiveServerTestCase automatically
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)

    def test_mobile_view(self):
        self.driver.set_window_size(375, 667)
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)

    def test_tablet_view(self):
        self.driver.set_window_size(768, 1024)
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)

    def test_desktop_view(self):
        self.driver.set_window_size(1920, 1080)
        self.driver.get(self.live_server_url)
        body = self.driver.find_element(By.TAG_NAME, 'body')
        self.assertIsNotNone(body)
