import json
import os
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from typing import Dict, List, Union

import boto3
import httpx
from aws_lambda_typing import context as lambda_context
from twilio.rest import Client


def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event: {event}")
        
        session_reminders_table_name = os.environ.get('SESSION_REMINDERS_TABLE_NAME')
        sessions_table_name = os.environ.get('SESSIONS_TABLE_NAME')
        students_table_name = os.environ.get('STUDENTS_TABLE_NAME')
        students_metadata_table_name = os.environ.get('STUDENTS_METADATA_TABLE_NAME')
        tutors_table_name = os.environ.get('TUTORS_TABLE_NAME')
        tutors_metadata_table_name = os.environ.get('TUTORS_METADATA_TABLE_NAME')
        discord_secret_arn = os.environ.get('DISCORD_SECRETS_ARN')
        secrets_arn = os.environ.get('SECRETS_ARN')
        
        secrets = get_secrets(secrets_arn)
        dynamodb = boto3.resource('dynamodb')
        
        session_reminders_table = dynamodb.Table(session_reminders_table_name)
        sessions_table = dynamodb.Table(sessions_table_name)
        students_table = dynamodb.Table(students_table_name)
        students_metadata_table = dynamodb.Table(students_metadata_table_name)
        tutors_table = dynamodb.Table(tutors_table_name)
        tutors_metadata_table = dynamodb.Table(tutors_metadata_table_name)
        
        twilio_client = Client(secrets['twilioAccountSid'], secrets['twilioAuthToken'])

        discord_secrets_client = boto3.client('secretsmanager')
        discord_secret_response = discord_secrets_client.get_secret_value(SecretId=discord_secret_arn)
        discord_creds = json.loads(discord_secret_response['SecretString'])
        discord_bot_token = discord_creds['bot_token']
        
        # Get current time and 5-hour window
        now_utc = datetime.now(timezone.utc)
        five_hours_later = now_utc + timedelta(hours=5)
        
        # Scan all sessions
        sessions = scan_all_sessions(sessions_table)
        print(f"Found {len(sessions)} total sessions")
        
        results = []
        for session in sessions:
            # Use UTC fields directly
            start_utc_str = session.get('utcStart')
            end_utc_str = session.get('utcEnd')
            
            if not start_utc_str or not end_utc_str:
                continue
            
            start_utc = datetime.fromisoformat(start_utc_str)
            end_utc = datetime.fromisoformat(end_utc_str)
            
            # Filter: start time between now and 5 hours from now
            if not (now_utc <= start_utc <= five_hours_later):
                continue
            
            summary = session.get('summary', '')
            session_id = session.get('sessionId', '')
            
            # Skip proposed sessions
            if 'proposed' in summary.lower():
                continue
            
            # Extract student name: everything before " Tutoring" (case-insensitive)
            match = re.search(r'^(.+?)\s+tutoring', summary, re.IGNORECASE)
            if not match:
                continue
            
            student_name = match.group(1).strip()
            
            # Get student from Students table
            try:
                student_response = students_table.get_item(Key={'studentName': student_name})
                student_metadata_response = students_metadata_table.get_item(Key={'studentName': student_name})
                if 'Item' not in student_response:
                    print(f"Student not found: {student_name}")
                    continue

                if 'Item' not in student_metadata_response:
                    print(f"Student metadata not found: {student_name}")
                    continue

                student = student_response['Item']
                student_metadata = student_metadata_response['Item']
            except Exception as e:
                print(f"Error fetching student {student_name}: {e}")
                continue

            iana_time_zone = student_metadata.get('studentTimezone', "unknown")

            if iana_time_zone == "unknown":
                print(f"Unknown timezone for student {student_name}")
                continue

            # Convert UTC to local timezone
            local_tz = ZoneInfo(iana_time_zone)
            start_dt = start_utc.astimezone(local_tz)
            end_dt = end_utc.astimezone(local_tz)
            
            # Get phone numbers with sessionReminders = true
            phone_numbers = []
            phone_numbers_map = student_metadata.get('phoneNumbers', {})

            for phone_number, settings in phone_numbers_map.items():
                if settings.get('sessionReminders') is True:
                    phone_numbers.append(phone_number)

            if not phone_numbers:
                print(f"No session reminder-enabled phone numbers for {student_name}")
                continue

            doc_url = student.get('docUrl', 'N/A')
            
            # Create UID using sessionId instead of summary to prevent duplicates on rename
            uid = f"{session_id}#{start_utc.isoformat()}#{end_utc.isoformat()}"
            
            # Check if reminder already exists
            try:
                reminder_response = session_reminders_table.get_item(Key={'uid': uid})
                existing_reminder = reminder_response.get('Item')
            except Exception:
                existing_reminder = None
            
            # Determine which phone numbers need SMS
            sms_sent = existing_reminder.get('sms_sent', {}) if existing_reminder else {}
            phones_to_send = []
            
            for phone in phone_numbers:
                if phone not in sms_sent or sms_sent.get(phone) == 'N/A':
                    phones_to_send.append(phone)
            
            if not phones_to_send:
                print(f"All SMS already sent for {summary}")
                continue
            
            # Format message
            start_pretty = start_dt.strftime('%I:%M %p').lstrip('0')
            end_pretty = end_dt.strftime('%I:%M %p').lstrip('0')
            message_body = f"Hello, this is a reminder for {summary} with MathPracs today from {start_pretty} to {end_pretty} {start_dt.strftime('%Z')}.\n\nMeeting info: {doc_url}."
            
            # Send SMS to each phone number
            for phone in phones_to_send:
                try:
                    message = twilio_client.messages.create(
                        body=message_body,
                        from_=secrets['twilioPhoneNumber'],
                        to=phone,
                        messaging_service_sid=None
                    )
                    sms_sent[phone] = message.sid
                    print(f"Sent SMS to {phone}: {message.sid}")
                except Exception as e:
                    sms_sent[phone] = 'N/A'
                    print(f"Failed to send SMS to {phone}: {e}")
            
            # Save/update reminder in DynamoDB
            reminder_item = {
                'uid': uid,
                'summary': summary,
                'start': start_dt.isoformat(),
                'end': end_dt.isoformat(),
                'start_utc': start_utc.isoformat(),
                'end_utc': end_utc.isoformat(),
                'tutorId': session.get('tutorId'),
                'sessionId': session_id,
                'status': session.get('status'),
                'sms_sent': sms_sent
            }
            
            if session.get('studentInfo'):
                reminder_item['studentInfo'] = session.get('studentInfo')
            
            session_reminders_table.put_item(Item=reminder_item)

            result = {
                'summary': summary,
                'student_name': student_name,
                'sms_sent_count': len([v for v in sms_sent.values() if v != 'N/A']),
                'discord_sent_count': 0  # Placeholder, will be updated in-place if discord message is sent
            }
            results.append(result)

            # Send session reminder to tutor discord channel as well
            try:
                tutor_id = session.get('tutorId')
                tutor_response = tutors_table.get_item(Key={'tutorId': tutor_id})
                tutor_metadata_response = tutors_metadata_table.get_item(Key={'tutorId': tutor_id})
                if 'Item' not in tutor_response:
                    print(f"Tutor with tutorId not found: {tutor_id}")
                    continue

                if 'Item' not in tutor_metadata_response:
                    print(f"Tutor metadata with tutorId not found: {tutor_id}")
                    continue

                tutor = tutor_response['Item']
                tutor_metadata = tutor_metadata_response['Item']
            except Exception as e:
                print(f"Error fetching tutor with tutorId {session.get('tutorId')}: {e}")
                continue

            tutor_name = tutor_metadata.get('tutorName', 'unknown')
            if tutor_name == "unknown":
                print(f"Warning: unknown tutorName for tutor with tutorId {tutor_id}")

            tutor_iana_time_zone = tutor_metadata.get('tutorTimezone', 'unknown')

            if tutor_iana_time_zone == "unknown":
                print(f"Unknown timezone for tutor {tutor_name}")
                continue

            # Convert UTC to local timezone for the tutor
            tutor_local_tz = ZoneInfo(tutor_iana_time_zone)
            tutor_start_dt = start_utc.astimezone(tutor_local_tz)
            tutor_end_dt = end_utc.astimezone(tutor_local_tz)

            tutor_existing_reminder = existing_reminder

            # Determine if discord message needs to be sent
            discord_sent = tutor_existing_reminder.get('discord_sent', False) if tutor_existing_reminder else False

            if discord_sent:
                print(f"Discord reminder message already sent for {summary}")
                continue

            # Format message
            tutor_start_pretty = tutor_start_dt.strftime('%B %d %Y @ ') + tutor_start_dt.strftime('%I:%M %p').lstrip('0')
            tutor_end_pretty = tutor_end_dt.strftime('%B %d %Y @ ') + tutor_end_dt.strftime('%I:%M %p').lstrip('0')
            tutor_message_body = f"Hello, this is a reminder for {summary} from {tutor_start_pretty} to {tutor_end_pretty} {tutor_start_dt.strftime('%Z')}.\n"

            # Get discord channel ID for the tutor
            discord_channel_id = tutor.get('sessionRemindersDiscordChannelId')

            if not discord_channel_id:
                print(f"No session reminder discord channel ID for tutor {tutor_name}")
                continue

            print(f"Sending Discord message: {tutor_message_body}")
            discord_sent_count = 0
            try:
                send_discord_message(discord_bot_token, discord_channel_id, tutor_message_body)
                session_reminders_table.update_item(
                    Key={'uid': uid},
                    UpdateExpression='SET discord_sent = :val',
                    ExpressionAttributeValues={':val': True}
                )
                discord_sent_count += 1

            except Exception as e:
                print(f"Failed to send Discord message: {e}")

            result['discord_sent_count'] = discord_sent_count
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'MathPracs Session Reminders executed successfully',
                'results': results
            })
        }
        
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }


def get_secrets(secrets_arn: str) -> Dict[str, str]:
    client = boto3.client('secretsmanager')
    response = client.get_secret_value(SecretId=secrets_arn)
    return json.loads(response['SecretString'])

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10), retry=retry_if_exception(lambda e: isinstance(e, httpx.HTTPError)))
def send_discord_message(discord_bot_token, discord_channel_id, message_body):
    response = httpx.post(
        f"https://discord.com/api/v10/channels/{discord_channel_id}/messages",
        headers={"Authorization": f"Bot {discord_bot_token}", "Content-Type": "application/json"},
        json={"content": message_body},
        timeout=30.0
    )
    response.raise_for_status()
    return response

def scan_all_sessions(table) -> List[Dict]:
    """Scan all items from Sessions table."""
    sessions = []
    response = table.scan()
    sessions.extend(response.get('Items', []))
    
    # Handle pagination
    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        sessions.extend(response.get('Items', []))
    
    return sessions
