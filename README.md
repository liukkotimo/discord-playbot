# playBot

Ddiscord bot to play music in voice channel from youtube. This is intended to be used in our private server so all is in finnish,
excep commands.

playbot soittaa Discordissa käyttäjän äänikanavalla YouTube-videoiden ääniraitoja.

## Komennot

| Komento | Kuvaus |
|---|---|
| `/play <URL>` | Lisää soittojonoon YouTube-videon tai -soittolistan. |
| `/queue` | Näyttää nykyisen soittojonon. |
| `/skip` | Ohittaa nykyisen kappaleen. |
| `/clear` | Tyhjennä soittojono. |
| `/shuffle` | Sekoittaa soittojonon (nykyinen kappale pysyy paikallaan). |
| | |
| **Soittolistat** | |
| `/playlists` | Näyttää kaikki tallennetut soittolistat. |
| `/create <nimi>` | Luo uuden, tyhjän soittolistan. |
| `/show_playlist <nimi>` | Näyttää tietyn soittolistan kappaleet. |
| `/delete_playlist <nimi>` | Poistaa soittolistan pysyvästi. |
| `/remove_from_playlist <nimi> <kappaleen_numero>` | Poistaa kappaleen soittolistalta sen numeron perusteella. |
| `/add_to_playlist <nimi>` | Lisää nykyisen soittojonon kappaleet soittolistaan (ei lisää duplikaatteja). |
| `/play_playlist <nimi>` | Lisää soittolistan kappaleet soittojonoosi (ei lisää duplikaatteja). |
