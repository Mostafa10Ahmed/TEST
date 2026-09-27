import re

with open('cogs/admin.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace all these classes with empty strings
classes_to_remove = [
    'AuthModal', 'SiteTokenModal', 'AsuraCookiesModal', 'AsuraCookiesView',
    'ImportAuthModal', 'RemoveKeyModal', 'ClearDomainModal', 'DomainSelect', 'AuthPanelV2View'
]

for cls in classes_to_remove:
    # Match class definition until the next class or async def setup/command
    pattern = r'class ' + cls + r'[\s\S]*?(?=\nclass |\n    @app_commands\.|\nasync def )'
    code = re.sub(pattern, '', code)

# Also remove commands
commands_to_remove = [
    'site_auth_cmd', 'site_token_cmd', 'asura_token_cmd', 'asura_cookies_cmd', 'auth_panel_v2_cmd', 'asura_test_cmd'
]

for cmd in commands_to_remove:
    pattern = r'    @app_commands\.command[\s\S]*?async def ' + cmd + r'[\s\S]*?(?=\n    @app_commands\.command|\nasync def )'
    code = re.sub(pattern, '', code)

with open('cogs/admin.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Cleaned admin.py")
