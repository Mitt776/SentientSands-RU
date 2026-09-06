import os
import sys
import subprocess
import urllib.request
import zipfile
import shutil

# Configuration
PYTHON_VERSION = "3.13.2"
EMBEDDED_ZIP_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = os.path.join(SCRIPT_DIR, "python")
REQUIREMENTS_PATH = os.path.join(SCRIPT_DIR, "requirements.txt")
PYTHON_TAG = "".join(PYTHON_VERSION.split(".")[:2])

def main():
    print(f"--- Setting up Embedded Python {PYTHON_VERSION} ---")
    
    if os.path.exists(TARGET_DIR):
        print(f"Target directory {TARGET_DIR} already exists. Cleaning it...")
        shutil.rmtree(TARGET_DIR)
        
    os.makedirs(TARGET_DIR)
    
    zip_path = os.path.join(SCRIPT_DIR, "python_embedded.zip")
    print(f"Downloading embedded Python from {EMBEDDED_ZIP_URL}...")
    urllib.request.urlretrieve(EMBEDDED_ZIP_URL, zip_path)
    
    print("Extracting...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(TARGET_DIR)
    os.remove(zip_path)
    
    # Fix .pth file to enable site-packages
    # Embedded python by default ignores site-packages and PYTHONPATH unless we fix the .pth file
    pth_file = os.path.join(TARGET_DIR, f"python{PYTHON_TAG}._pth")
    if os.path.exists(pth_file):
        print(f"Updating {pth_file} to enable site-packages...")
        with open(pth_file, 'r') as f:
            lines = f.readlines()
        
        # Uncomment 'import site' if it's there, or add it
        with open(pth_file, 'w') as f:
            for line in lines:
                if line.strip() == "#import site":
                    f.write("import site\n")
                else:
                    f.write(line)
            # Ensure it's there
            if not any("import site" in l for l in lines):
                f.write("import site\n")

    print("Downloading get-pip.py...")
    pip_loader = os.path.join(TARGET_DIR, "get-pip.py")
    urllib.request.urlretrieve(GET_PIP_URL, pip_loader)
    
    print("Installing pip...")
    python_exe = os.path.join(TARGET_DIR, "python.exe")
    subprocess.run([python_exe, pip_loader], check=True)
    os.remove(pip_loader)
    
    print("Installing requirements...")
    subprocess.run([python_exe, "-m", "pip", "install", "-r", REQUIREMENTS_PATH], check=True)
    
    print("--- Embedded Python Setup Complete ---")
    print(f"Location: {TARGET_DIR}")

if __name__ == "__main__":
    main()
