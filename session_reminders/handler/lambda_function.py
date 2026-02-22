import json
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Union

import boto3
from aws_lambda_typing import context as lambda_context
from twilio.rest import Client


def lambda_handler(event: Dict[str, Union[str, int, float, bool, None]], context: lambda_context.Context) -> Dict[str, Union[str, int]]:
    try:
        print(f"Received Event: {event}")
        
        session_reminders_table_name = os.environ.get('SESSION_REMINDERS_TABLE_NAME')
        sessions_table_name = os.environ.get('SESSIONS_TABLE_NAME')
        students_table_name = os.environ.get('STUDENTS_TABLE_NAME')
        secrets_arn = os.environ.get('SECRETS_ARN')
        
        secrets = get_secrets(secrets_arn)
        dynamodb = boto3.resource('dynamodb')
        
        session_reminders_table = dynamodb.Table(session_reminders_table_name)
        sessions_table = dynamodb.Table(sessions_table_name)
        students_table = dynamodb.Table(students_table_name)
        
        twilio_client = Client(secrets['twilioAccountSid'], secrets['twilioAuthToken'])
        
        # Get current time and 4-hour window
        now_utc = datetime.now(timezone.utc)
        four_hours_later = now_utc + timedelta(hours=4)
        
        # Scan all sessions
        sessions = scan_all_sessions(sessions_table)
        print(f"Found {len(sessions)} total sessions")
        
        results = []
        for session in sessions:
            # Use UTC fields directly
            start_utc_str = session.get('utcStart')
            end_utc_str = session.get('utcEnd')
            iana_time_zone = session.get('timezone')
            
            if not start_utc_str or not end_utc_str or not iana_time_zone:
                continue
            
            start_utc = datetime.fromisoformat(start_utc_str)
            end_utc = datetime.fromisoformat(end_utc_str)
            
            # Convert UTC to local timezone
            from zoneinfo import ZoneInfo
            local_tz = ZoneInfo(iana_time_zone)
            start_dt = start_utc.astimezone(local_tz)
            end_dt = end_utc.astimezone(local_tz)
            
            # Filter: start time between now and 4 hours from now
            if not (now_utc <= start_utc <= four_hours_later):
                continue
            
            summary = session.get('summary', '')
            
            # Extract student name by removing " Tutoring"
            student_name = summary.replace(' Tutoring', '').replace(' tutoring', '')
            
            if not student_name:
                continue
            
            # Get student from Students table
            try:
                student_response = students_table.get_item(Key={'studentName': student_name})
                if 'Item' not in student_response:
                    print(f"Student not found: {student_name}")
                    continue
                
                student = student_response['Item']
            except Exception as e:
                print(f"Error fetching student {student_name}: {e}")
                continue
            
            # Get phone numbers with smsEnabled = true
            phone_numbers = []
            for i in range(1, 6):
                number_key = f'number{i}'
                if number_key in student:
                    number_obj = student[number_key]
                    if number_obj.get('smsEnabled') is True:
                        phone_numbers.append(number_obj.get('phoneNumber'))
            
            if not phone_numbers:
                print(f"No SMS-enabled phone numbers for {student_name}")
                continue
            
            doc_url = student.get('docUrl', 'N/A')
            
            # Create UID
            uid = f"{summary}#{start_utc.isoformat()}#{end_utc.isoformat()}"
            
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
                if phone not in sms_sent:
                    phones_to_send.append(phone)
            
            if not phones_to_send:
                print(f"All SMS already sent for {summary}")
                continue
            
            # Format message
            start_pretty = start_dt.strftime('%I:%M %p').lstrip('0')
            end_pretty = end_dt.strftime('%I:%M %p').lstrip('0')
            message_body = f"(AWS) Hello, this is a reminder for {summary} with MathPracs today from {start_pretty} to {end_pretty}.\n\nMeeting info: {doc_url}."
            
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
                'sessionId': session.get('sessionId'),
                'status': session.get('status'),
                'sms_sent': sms_sent
            }
            
            if session.get('studentInfo'):
                reminder_item['studentInfo'] = session.get('studentInfo')
            
            session_reminders_table.put_item(Item=reminder_item)
            
            results.append({
                'summary': summary,
                'student_name': student_name,
                'sms_sent_count': len([v for v in sms_sent.values() if v != 'N/A'])
            })
        
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
