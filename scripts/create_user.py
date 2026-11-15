"""Create a Cognito user in a role group. Cognito emails an invite with a temporary password;
at first sign-in the hosted UI forces a new password and TOTP enrollment (MFA is required).

Usage: uv run python scripts/create_user.py USER_POOL_ID EMAIL viewer|approver|admin [--region us-west-2]
"""
import sys

import boto3

pool, email, group = sys.argv[1:4]
region = sys.argv[sys.argv.index("--region") + 1] if "--region" in sys.argv else "us-west-2"
if group not in {"viewer", "approver", "admin"}:
    sys.exit("group must be viewer, approver or admin")
c = boto3.client("cognito-idp", region_name=region)
c.admin_create_user(UserPoolId=pool, Username=email, DesiredDeliveryMediums=["EMAIL"],
                    UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
c.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=group)
print(f"created {email} in group {group}; an invite email was sent")
