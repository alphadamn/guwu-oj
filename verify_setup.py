#!/usr/bin/env python
"""
Post-clone verification script for guwu-oj
Checks database, Redis, Docker, and system configuration
"""

import os
import sys
import subprocess
import django
from pathlib import Path

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')
django.setup()

from django.conf import settings
from django.db import connection
from django.core.cache import cache
import redis
import docker


def print_header(text):
    """Print a formatted header"""
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")


def print_success(message):
    """Print success message"""
    print(f"✓ {message}")


def print_error(message):
    """Print error message"""
    print(f"✗ {message}")


def print_warning(message):
    """Print warning message"""
    print(f"⚠ {message}")


def check_python_version():
    """Check Python version"""
    print_header("Python Version Check")
    version = sys.version_info
    print(f"Python version: {version.major}.{version.minor}.{version.micro}")
    
    if version.major == 3 and version.minor >= 9:
        print_success("Python version is compatible (3.9+)")
        return True
    else:
        print_error("Python version must be 3.9 or higher")
        return False


def check_database_connection():
    """Check database connection and configuration"""
    print_header("Database Connection Check")
    
    try:
        # Check database configuration
        db_config = settings.DATABASES['default']
        print(f"Database engine: {db_config['ENGINE']}")
        print(f"Database name: {db_config['NAME']}")
        print(f"Database host: {db_config.get('HOST', 'localhost')}")
        print(f"Database port: {db_config.get('PORT', '5432')}")
        
        # Test connection
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
            if result and result[0] == 1:
                print_success("Database connection successful")
                return True
            else:
                print_error("Database connection test failed")
                return False
                
    except Exception as e:
        print_error(f"Database connection failed: {e}")
        return False


def check_database_tables():
    """Check if all required database tables exist"""
    print_header("Database Tables Check")
    
    try:
        with connection.cursor() as cursor:
            # Get list of tables
            cursor.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
                ORDER BY table_name
            """)
            tables = [row[0] for row in cursor.fetchall()]
            
            print(f"Found {len(tables)} tables in database:")
            for table in tables:
                print(f"  - {table}")
            
            # Check for essential tables
            required_tables = [
                'users_user',
                'problems_problem', 
                'submissions_submission',
                'django_migrations'
            ]
            
            missing_tables = [t for t in required_tables if t not in tables]
            
            if missing_tables:
                print_error(f"Missing required tables: {missing_tables}")
                return False
            else:
                print_success("All required database tables exist")
                return True
                
    except Exception as e:
        print_error(f"Database table check failed: {e}")
        return False


def check_redis_connection():
    """Check Redis connection and configuration"""
    print_header("Redis Connection Check")
    
    try:
        # Get Redis URL from cache configuration
        cache_config = settings.CACHES['default']
        redis_url = cache_config['LOCATION']
        print(f"Redis URL: {redis_url}")
        
        # Parse Redis URL
        if redis_url.startswith('redis://'):
            # Extract host and port
            url_parts = redis_url.replace('redis://', '').split('/')
            host_port = url_parts[0].split(':')
            host = host_port[0]
            port = int(host_port[1]) if len(host_port) > 1 else 6379
            db = int(url_parts[1]) if len(url_parts) > 1 else 0
            
            print(f"Redis host: {host}")
            print(f"Redis port: {port}")
            print(f"Redis database: {db}")
            
            # Test connection
            r = redis.Redis(host=host, port=port, db=db, decode_responses=True)
            r.ping()
            print_success("Redis connection successful")
            
            # Test cache operations
            cache.set('test_key', 'test_value', 10)
            value = cache.get('test_key')
            if value == 'test_value':
                print_success("Redis cache operations working")
                cache.delete('test_key')
                return True
            else:
                print_error("Redis cache operations failed")
                return False
        else:
            print_error("Invalid Redis URL format")
            return False
            
    except Exception as e:
        print_error(f"Redis connection failed: {e}")
        return False


def check_docker_status():
    """Check Docker status and availability"""
    print_header("Docker Status Check")
    
    try:
        import docker
        from docker.errors import DockerException
        
        # Check if Docker is installed and running
        client = docker.from_env()
        version = client.version()
        print(f"Docker version: {version['Version']}")
        print(f"Docker API version: {version['ApiVersion']}")
        print_success("Docker is installed and running")
        
        # Check if judge image exists
        try:
            images = client.images.list()
            # print(images[0].tags)
            judge_images = [img for img in images if img.tags[0] in ['oj-cpp:latest','oj-c:latest','oj-python:latest','oj-java:latest','oj-other:latest']]
            
            if judge_images:
                print_success(f"Found {len(judge_images)} judge Docker image(s)")
                for img in judge_images:
                    print(f"  - {', '.join(img.tags)}")
            else:
                print_warning("No judge Docker image found. Run: cd docker/judge && docker build -t oj-judge:latest .")
            
            return True
            
        except Exception as e:
            print_warning(f"Could not check Docker images: {e}")
            return True  # Docker is working, just can't list images
            
    except ImportError:
        print_warning("Docker Python package is not installed. Run: pip install docker")
        print_warning("Docker is required for judge sandbox. Install Docker to enable judging.")
        return False
    except DockerException as e:
        print_error(f"Docker is not available: {e}")
        print_warning("Docker is required for judge sandbox. Install Docker to enable judging.")
        return False
    except Exception as e:
        print_error(f"Docker check failed: {e}")
        return False


def check_docker_safety():
    """Check Docker security configuration"""
    print_header("Docker Safety Check")
    
    try:
        import docker
        from docker.errors import DockerException
        
        client = docker.from_env()
        
        # Check if running in a container
        if os.path.exists('/.dockerenv'):
            print_warning("Running inside a Docker container")
        
        # Check for exposed Docker socket (security risk)
        if os.path.exists('/var/run/docker.sock'):
            print_warning("Docker socket is exposed. This may be a security risk in production.")
        
        # Check if running as root
        if os.geteuid() == 0:
            print_warning("Running as root. Consider running with a non-root user for better security.")
        
        print_success("Docker safety check completed")
        return True
        
    except ImportError:
        print_warning("Docker Python package is not installed. Skipping safety check.")
        return True  # Not a failure, just skip
    except DockerException as e:
        print_warning(f"Docker is not available: {e}. Skipping safety check.")
        return True  # Not a failure, just skip
    except Exception as e:
        print_error(f"Docker safety check failed: {e}")
        return False


def check_dependencies():
    """Check if required Python packages are installed"""
    print_header("Python Dependencies Check")
    
    required_packages = [
        'django',
        'psycopg2',
        'redis',
        'django-redis',
        'django-rq',
        'docker',
    ]
    
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package.replace('-', '_'))
            print_success(f"{package} is installed")
        except ImportError:
            print_error(f"{package} is not installed")
            missing_packages.append(package)
    
    if missing_packages:
        print_error(f"Missing packages: {', '.join(missing_packages)}")
        print("Run: pip install -r requirements.txt")
        return False
    else:
        print_success("All required dependencies are installed")
        return True


def check_file_permissions():
    """Check file permissions for logs and static files"""
    print_header("File Permissions Check")
    
    # Check logs directory
    logs_dir = Path('logs')
    if logs_dir.exists():
        if os.access(logs_dir, os.W_OK):
            print_success("Logs directory is writable")
        else:
            print_error("Logs directory is not writable")
            return False
    else:
        print_warning("Logs directory does not exist. It will be created automatically.")
    
    # Check static files directory
    staticfiles_dir = Path('staticfiles')
    if staticfiles_dir.exists():
        if os.access(staticfiles_dir, os.W_OK):
            print_success("Static files directory is writable")
        else:
            print_error("Static files directory is not writable")
            return False
    
    print_success("File permissions check completed")
    return True


def check_environment_variables():
    """Check critical environment variables"""
    print_header("Environment Variables Check")
    
    critical_vars = {
        'DJANGO_SECRET_KEY': settings.SECRET_KEY,
        'DEBUG': settings.DEBUG,
    }
    
    for var_name, var_value in critical_vars.items():
        if var_value:
            if var_name == 'DJANGO_SECRET_KEY' and var_value == 'django-insecure-change-me':
                print_warning(f"{var_name} is using default value. Change it in production!")
            else:
                print_success(f"{var_name} is set")
        else:
            print_error(f"{var_name} is not set")
    
    print_success("Environment variables check completed")
    return True


def main():
    """Run all verification checks"""
    print_header("Guhu-OJ Setup Verification")
    print("This script checks your environment configuration after cloning.\n")
    
    results = []
    
    # Run all checks
    results.append(("Python Version", check_python_version()))
    results.append(("Dependencies", check_dependencies()))
    results.append(("Database Connection", check_database_connection()))
    results.append(("Database Tables", check_database_tables()))
    results.append(("Redis Connection", check_redis_connection()))
    results.append(("Docker Status", check_docker_status()))
    results.append(("Docker Safety", check_docker_safety()))
    results.append(("File Permissions", check_file_permissions()))
    results.append(("Environment Variables", check_environment_variables()))
    
    # Print summary
    print_header("Verification Summary")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for check_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {check_name}")
    
    print(f"\nTotal: {passed}/{total} checks passed")
    
    if passed == total:
        print_success("\n🎉 All checks passed! Your environment is properly configured.")
        return 0
    else:
        print_error(f"\n⚠ {total - passed} check(s) failed. Please fix the issues above.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
