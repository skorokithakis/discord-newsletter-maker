# Discord newsletter maker

This is a small set of scripts that uses tyrrrz/discordchatexporter to create a newsletter with all the links in Discord.

To run:

* `docker run -v ./out/:/out tyrrrz/discordchatexporter exportguild --token <token> --after <date> --guild <id> -f JSON --include-vc False --include-threads All`
* `./gather_links.py`
* `./newsletter.py messages_with_links.txt`
* `./send_campaign.py --subject "The latest links" <list id> <template>.html`
