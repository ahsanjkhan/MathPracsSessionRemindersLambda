### What Is This

This is the implementation of an AWS Lambda Function which is defined in the https://github.com/ahsanjkhan/MathPracsSessionRemindersCDK repository.

The purpose of this Lambda is to process automated session text message reminders for students enrolled in tutoring with MathPracs.

You can learn more about MathPracs at https://mathpracs.com

### How Does It Work

The Lambda is invoked by an AWS EventBridge Scheduler Rule every 3 minutes.

It scans the Sessions DynamoDB table for sessions starting within the next 4 hours.

For each upcoming session, it queries the Students DynamoDB table to get phone numbers with SMS enabled.

Finally, it integrates with Twilio to send out the text message reminders and tracks sent messages in DynamoDB to prevent duplicates.

### What Are The Components

AWS Lambda, AWS DynamoDB, AWS EventBridge Scheduler, AWS SecretsManager, Twilio API.
