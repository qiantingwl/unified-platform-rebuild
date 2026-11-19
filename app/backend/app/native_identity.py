"""Official account display fields, with sensitive numeric/email parts masked."""
import re

INTERNAL=('迁入验收','副本验收','线上测试','沙盒')

def safe_label(value):
    if not isinstance(value,str) or not value.strip() or any(x in value for x in INTERNAL):return None
    value=re.sub(r'^(?:Loomy|小浣熊|WPS\s*灵犀|WorkBuddy\s*(?:国内|国际)|CodeArts|Marvis)\s*[·:：-]?\s*(?:迁入|导入)\s*[·:：-]?\s*', '', value.strip())
    if not value:return None
    value=re.sub(r'(?<!\d)(?:\+?86)?(1\d{2})\d{4}(\d{4})(?!\d)',r'\1****\2',value.strip())
    value=re.sub(r'([\w.+-])([\w.+-]+)(@[\w.-]+)',r'\1***\3',value)
    return value[:100]

def capture_identity(account,credential):
    profile=credential.get('account') if isinstance(credential.get('account'),dict) else {}
    profile=profile or (credential.get('profile') if isinstance(credential.get('profile'),dict) else {})
    for item in (profile.get('displayName'),profile.get('display_name'),profile.get('nickname'),profile.get('nickName'),profile.get('name'),credential.get('display_name'),credential.get('nickname')):
        value=safe_label(item)
        if value:
            account.original_label=value
            break
    return account.original_label
