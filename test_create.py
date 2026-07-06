import traceback
from create_account import create_zai_account
print("=== Starting single account creation test ===")
try:
    result = create_zai_account()
    print("Result:", result)
except Exception as e:
    print("EXCEPTION DURING CREATE:")
    traceback.print_exc()