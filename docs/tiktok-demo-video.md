# TikTok app review: recording the demo video

TikTok's app review asks for "a demo video that shows the complete
end-to-end flow of the integration with TikTok". This is a screen
recording of Axionory, from connecting a TikTok account to a posted
video. You don't need to generate a new HeyGen or ElevenLabs video for
it; post a video you have already approved.

## Before recording

- [ ] Deployed: TikTok connect and posting (PR #56) and the
      connection-row fix for existing websites (PR #57).
- [ ] At least one **approved** video in Approvals, for example
      Road2CISSP with Jerrod as presenter.
- [ ] Do one trial run first (connect, then a private test post), so
      you don't film an error. You can delete the test post in TikTok
      afterwards.
- [ ] Use **www.axionory.com**, not ventoryn.app. TikTok only returns
      to www.axionory.com.
- [ ] Have your authenticator app ready for the 2FA code.
- [ ] Close tabs and notifications that you don't want on screen.

How to record on Windows: Snipping Tool (`Win + Shift + R`), choose
**Video**, then select the browser window. Aim for one take of 1 to 2
minutes.

## Shot list

1. **Signed in to www.axionory.com.** Open **Integrations**, then click
   the **TikTok** card.
2. Click **Connect** on **Road2CISSP**, then **Connect with TikTok**.
3. **TikTok's authorization screen.** Sign in and leave the requested
   permissions on screen for a moment (user info, video publish and
   video upload), then click **Authorize**.
4. **Back in Axionory.** The Road2CISSP window shows
   **Connected as ...**.
5. Open **Approvals**, scroll to an approved video, and click
   **Post to TikTok**.
6. **The posting screen.** Pause briefly on each item, because this is
   what the reviewers check:
   - the TikTok nickname: "Posting to TikTok as ..."
   - the video preview, played for a second or two
   - the title: type a short caption
   - **Who can view this video**: open the menu (nothing is
     pre-selected) and pick **Only me**
   - **Allow viewers to**: Comment, Duet and Stitch (unchecked;
     greyed out if they are turned off in your TikTok settings)
   - **Content disclosure**: turn on "This video promotes a brand,
     product or service" to show **Your brand** and **Branded content**
     and the label notes, then turn it off again
   - **Label as AI-generated content**: left checked
   - the line "By posting, you agree to TikTok's Music Usage
     Confirmation"
7. Enter the 2FA code and click **Post to TikTok**.
8. **The status page.** It checks every 10 seconds; wait until it shows
   **Posted on TikTok**.
9. **TikTok itself** (tiktok.com or the app): show the new private
   video on the profile.

Optional, if the form asks for disconnect or revoke: go to
**Integrations, TikTok, Road2CISSP** and click **Disconnect**. This
revokes Axionory's access at TikTok.

## Why the posts are private for now

Until TikTok approves the app, the Content Posting API only accepts
private posts (**Only me**). The posting screen offers only that
option. Once the audit passes, set `TIKTOK_AUDITED=1` in
`.env.ventoryn` on the server and redeploy; Everyone, Friends and
Followers then appear as choices.

## If something fails

- **Error after Authorize** (the token exchange): copy the message on
  the screen. The token and revoke calls follow TikTok's standard v2
  form, but the docs pages couldn't be read when this was built (see
  `ventoryn/tiktok.py`), so this is the first real test of them.
- **Status FAILED**: the status page shows TikTok's reason. A reason
  about the URL or domain means TikTok could not fetch the video. Check
  that `https://www.axionory.com/` (or the `/media/tiktok/` prefix) is
  verified under URL properties in the TikTok developer portal.
- **"Too many posts" or a spam-risk message**: TikTok limits how often
  one account can post. Wait and try again later.
