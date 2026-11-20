# প্রথম টেস্ট (ফেসবুক ছাড়া)

Secrets (শুধু এই তিনটা): GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
(চাইলে POLLINATIONS_KEY — ফ্রি, ওয়াটারমার্ক ও ১৫ সেকেন্ডের সীমা সরায়)
config.json-এ facebook_enabled আপাতত false রাখো। PAGE_ID আর PAGE_TOKEN পরে বসাবে।

ধাপ:
1. রিপোজিটরিতে সব ফাইল আপলোড → Actions → reels → Run workflow
2. টেলিগ্রামে /help পাঠাও, ১০-১৫ মিনিটের মধ্যে উত্তর আসবে
3. /make page1 1 → প্রিভিউ ভিডিও আসবে (কয়েক মিনিট লাগে)
4. নিজের ভিডিও পাঠাও, ক্যাপশনে page2 লিখে → বট টাইটেল-ডিসক্রিপশন লিখে কিউতে রাখবে
5. /queue, /schedule page1 18:00, /redo, /skip পরীক্ষা করো
6. সব ঠিক থাকলে: PAGE_ID বসাও, PAGE_TOKEN_n Secrets-এ দাও, facebook_enabled: true করো
   (শুরুতে পেজ-প্রতি দিনে ১-২টা পোস্ট দিয়ে ধীরে বাড়াও)
