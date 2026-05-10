import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


DISCORD_API_BASE = "https://discord.com/api/v10"
LOG_GROUP = "/aws/lambda/mathpracs-session-reminder"
REGION = "us-east-1"


def lambda_handler(event, context):
    discord_secret_arn = os.environ.get('DISCORD_SECRETS_ARN')
    secrets_client = boto3.client('secretsmanager')
    secret_response = secrets_client.get_secret_value(SecretId=discord_secret_arn)
    discord_creds = json.loads(secret_response['SecretString'])
    bot_token = discord_creds['bot_token']
    channel_id = discord_creds['session_reminders_channel_id']

    for record in event.get('Records', []):
        message = json.loads(record['Sns']['Message'])

        metric_name = message.get('Trigger', {}).get('MetricName', 'Unknown')
        dimensions = message.get('Trigger', {}).get('Dimensions', [])
        reason = next((d['value'] for d in dimensions if d['name'] == 'Reason'), 'N/A')
        timestamp = message.get('StateChangeTime', '')

        logs_url = build_logs_url(timestamp)

        notification = (
            f"🚨 **SessionReminders Alarm: {metric_name}**\n"
            f"Reason: {reason}\n"
            f"Time: {timestamp}\n\n"
            f"📋 Logs: {logs_url}"
        )

        send_discord_message(bot_token, channel_id, notification)

    return {"statusCode": 200}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPError)))
def send_discord_message(bot_token: str, channel_id: str, message_body: str):
    response = httpx.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
        json={"content": message_body},
        timeout=10.0
    )
    response.raise_for_status()
    return response


def build_logs_url(timestamp_str: str) -> str:
    try:
        ts = datetime.fromisoformat(timestamp_str.replace('+0000', '+00:00'))
    except (ValueError, AttributeError):
        ts = datetime.now(timezone.utc)

    start_ms = int((ts - timedelta(minutes=2)).timestamp() * 1000)
    end_ms = int((ts + timedelta(minutes=2)).timestamp() * 1000)

    encoded_log_group = quote(LOG_GROUP, safe='')
    return (
        f"https://console.aws.amazon.com/cloudwatch/home?region={REGION}"
        f"#logsV2:log-groups/log-group/{encoded_log_group}"
        f"/log-events?start={start_ms}&end={end_ms}"
    )
