import os
import re

base_path = 'tmp/scripts_new/scripts/'
dirs = sorted([d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d)) and d != '__pycache__'])

for slug in dirs:
    main_path = os.path.join(base_path, slug, 'main.py')
    if not os.path.exists(main_path):
        print(f"SLUG: {slug} | NO MAIN.PY")
        continue
    
    with open(main_path, 'r', errors='ignore') as f:
        content = f.read()
    
    # 1. Look for URLs
    urls = re.findall(r'https?://[^\s\'"]+', content)
    
    # 2. Look for inputs (argparse)
    input_args = re.findall(r'--([a-zA-Z0-9_-]+)', content)
    
    # 3. Look for imports (AI, Azure, etc)
    ai_calls = re.findall(r'openai|anthropic|Claude', content, re.I)
    azure_calls = re.findall(r'azure|blob', content, re.I)
    selenium_calls = re.findall(r'selenium|playwright|webdriver', content, re.I)
    
    # Also check parser files if they exist
    parser_dir = os.path.join(base_path, slug, 'parser')
    if os.path.exists(parser_dir):
        for pfile in os.listdir(parser_dir):
            if pfile.endswith('.py'):
                with open(os.path.join(parser_dir, pfile), 'r', errors='ignore') as f:
                    pcontent = f.read()
                    urls.extend(re.findall(r'https?://[^\s\'"]+', pcontent))
                    ai_calls.extend(re.findall(r'openai|anthropic|Claude', pcontent, re.I))
                    azure_calls.extend(re.findall(r'azure|blob', pcontent, re.I))
                    selenium_calls.extend(re.findall(r'selenium|playwright|webdriver', pcontent, re.I))

    # Also check other py files in the slug dir
    for sfile in os.listdir(os.path.join(base_path, slug)):
        if sfile.endswith('.py') and sfile != 'main.py':
             with open(os.path.join(base_path, slug, sfile), 'r', errors='ignore') as f:
                scontent = f.read()
                urls.extend(re.findall(r'https?://[^\s\'"]+', scontent))
                ai_calls.extend(re.findall(r'openai|anthropic|Claude', scontent, re.I))
                azure_calls.extend(re.findall(r'azure|blob', scontent, re.I))
                selenium_calls.extend(re.findall(r'selenium|playwright|webdriver', scontent, re.I))

    unique_urls = list(set(urls))
    ts_flag = any('tournamentsoftware.com' in u for u in unique_urls)
    
    print(f"SLUG: {slug}")
    print(f"  URLS: {unique_urls[:5]}")
    print(f"  TS: {ts_flag}")
    print(f"  ARGS: {list(set(input_args))}")
    print(f"  AI: {len(ai_calls) > 0}")
    print(f"  AZURE: {len(azure_calls) > 0}")
    print(f"  BROWSER: {len(selenium_calls) > 0}")
    print("-" * 20)
