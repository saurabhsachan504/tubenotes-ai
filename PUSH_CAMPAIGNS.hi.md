# Browser Push Campaign Scheduler

Admin panel (`/admin`) mein **Daily Campaign** aur **Weekly Campaign** ki copy,
time, weekday, cooldown aur enabled state set karein. Dono campaign sirf active
free users ko target karte hain jinke paas active browser-push subscription hai.
Pro/subscribed aur complimentary-Pro accounts live query mein automatically
exclude hote hain.

Production deployment ke baad server host ke root crontab mein ek once-per-minute
entry add karein. Campaign ka IST time admin panel se control hota hai; cron har
minute check karta hai aur exact minute par hi run queue karta hai.

```cron
* * * * * docker exec tubenotes-ai-app python -m app.jobs.push_campaigns --due >> /var/log/tubenotes-push-campaigns.log 2>&1
```

Cron duplicate safe hai: ek campaign/date ka scheduled run database unique key se
sirf ek baar banta hai. Manual admin-panel run alag run history row banata hai.

`ntfy` ko customers ke messages nahi milte. Run complete hone par sirf admin ko
eligible users, endpoints, sent, failed aur expired endpoint summary milti hai.
