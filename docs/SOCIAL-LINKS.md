# Linking an article from social media

**Every mikehammett.net link in a social post carries all four UTM parameters.** That covers LinkedIn, X, Facebook, Buffer and Reddit, and it applies to a post made by a person, by a Code session or through a browser tool alike. An untagged link reaches GA4 as direct traffic, and GA4 is the only measurement there is: Buffer's free plan has no analytics. On 2026-11-15 queued Thirteen Records posts were found with bare links, and on 2026-11-27 the `biometric-privacy-repeal` posts went out untagged. In both cases the rule existed in notes and nobody applied it.

## The scheme

Lowercase, with hyphens. Never mix in capitals or underscores in a value.

| Parameter | Value |
|---|---|
| `utm_source` | the platform only, never the account: `linkedin`, `facebook`, `twitter` (this is X), `reddit`. `tbw-linkedin` is wrong, because GA4 sorts by source |
| `utm_medium` | always `social` |
| `utm_campaign` | the article's **exact WordPress slug**. Take it from the published URL, not from the repo folder name, which can differ |
| `utm_content` | always present, and says whose post it is: `personal` for Mike's own LinkedIn, X and Facebook profiles; `tbw` for a TBW account; `group-<name>` for a Facebook group; `r-<subreddit>` for Reddit |

The platform is the source and the account is the content. Telling the two apart is the whole reason `utm_content` is never left off.

Examples, for the article `biometric-privacy-repeal`:

```
https://www.mikehammett.net/biometric-privacy-repeal/?utm_source=linkedin&utm_medium=social&utm_campaign=biometric-privacy-repeal&utm_content=personal
https://www.mikehammett.net/biometric-privacy-repeal/?utm_source=twitter&utm_medium=social&utm_campaign=biometric-privacy-repeal&utm_content=personal
https://www.mikehammett.net/biometric-privacy-repeal/?utm_source=facebook&utm_medium=social&utm_campaign=biometric-privacy-repeal&utm_content=personal
```

## Before and after posting

- **Read the final text in the composer and confirm the tags are in it before you post.** Do not rely on having written them into a draft elsewhere.
- **Check the saved link after posting.** LinkedIn rewrites links to `lnkd.in` and X to `t.co`, so the post shows a short link. Following the short link (`curl -sIL`) must land on the tagged URL.
- **Fixing a post that went out bare:** LinkedIn and Facebook posts can be edited at any time. X allows an edit only for about an hour after posting; an older X post has to be deleted and posted again.
- **Do not invent a scheme.** If a channel is not listed above, search past sessions and this document first, then add the channel's row here once a value is agreed.

## Where this came from

The scheme was settled in a DeKalb County property-records session on 2026-11-24 and was not written down in the repository until this page. The rule that every link is tagged is Mike's standing instruction.
