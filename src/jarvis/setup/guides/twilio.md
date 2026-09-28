# Phone calls: Twilio

Jarvis answers a Twilio number and calls you back from it.

1. **Sign up** at <https://www.twilio.com/try-twilio>. A trial account works, with two
   limits: it can only call numbers you have verified
   (<https://console.twilio.com/us1/develop/phone-numbers/manage/verified>), and every
   call opens with a short trial message.
2. **Get a number** with voice: <https://console.twilio.com/us1/develop/phone-numbers/manage/search>.
3. **Copy your Account SID and Auth Token** from the console's front page,
   <https://console.twilio.com/>. Jarvis checks them with one read-only request and lists
   your numbers to choose from.

Jarvis sets the number's two webhooks itself, once you have said yes to the exact
addresses. Texting stays off unless you turn it on: many accounts cannot send SMS in their
region, and Slack is the written channel.
