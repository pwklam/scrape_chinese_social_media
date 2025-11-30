import asyncio
import json
import re
from playwright.async_api import async_playwright
from datetime import datetime, timedelta
from logging_config import get_logger
import utils
import config

logger = get_logger()

async def scrape_post(url, conn):
    """
    Scrapes the title, publish date, content, and interaction counts 
    (Share, Comment, Like) for a specific Weibo post using Playwright.
    """
    logger.info("🚀 Launching Playwright browser...")
    logger.info(f"Target url: {url}")

    async with async_playwright() as p:
        # Launch chromium browser. Note: headless=False allows you to see and manually log in.
        # If you are already logged in, you can try setting headless=True.
        browser = await p.chromium.launch(headless=True, args=['--start-maximized'])
        
        # Create a new page context to maintain login state (e.g., cookies)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True
        )
        page = await context.new_page()

        try:
            # await page.goto(url, wait_until="networkidle")
            await page.goto(url)
            
            # --- Login Check and Wait (Crucial Step) ---
            # If the page redirects to the login screen, you must manually log in in the opened browser
            await page.wait_for_timeout(5000)
            
            # Check if the login page is displayed (e.g., look for a login box class)
            if await page.locator(".login_box").count() > 0:
                 logger.info("\n⚠️ Browser is open. Please manually complete the login within 50 seconds!")
                 # Give enough time for manual login
                 await page.wait_for_timeout(50000) 
                 await page.goto(url, wait_until="networkidle")
                 logger.info("✅ Login time finished, attempting to reload the post page...")
            
            # ------------------------------------

            # Wait for the main post content to load. This waits for the parent container of the post text.
            await page.wait_for_selector(".detail_wbtext_4CRf9", timeout=10000)
            logger.info("🔍 Page content loaded, starting data extraction...")

            title_locator = page.locator(".head_name_24eEB")
            title = await title_locator.inner_text()

            # 1. Post Content (also used as "Title")
            content_locator = page.locator(".detail_wbtext_4CRf9")
            content = await content_locator.inner_text()
            
            # 2. Publish Date
            date_locator = page.locator(".head-info_time_6sFQg")
            original_publish_date = await date_locator.inner_text()

            # START DATE FORMATTING LOGIC
            # Convert date format: 'YY-MM-DD HH:MM' -> 'YYYY-MM-DD HH:MM:SS'
            try:
                # Parse the date string based on the user-provided example format
                dt_object = datetime.strptime(original_publish_date.strip(), '%y-%m-%d %H:%M')
                # Format to YYYY-MM-DD HH:MM:SS (adding ':00' for seconds)
                formatted_date = dt_object.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                # If parsing fails (e.g., if the date is a relative time like '5 minutes ago'), 
                # keep the original text.
                logger.info(f"⚠️ Warning: Could not parse date '{original_publish_date.strip()}'. Keeping original format.")

            publish_date = formatted_date # Use the newly formatted date

            # 3. Interaction Counts (Share, Comment, Like)
            # The selectors here are based on the new weibo.com structure and may change with site updates
            # We locate the counts by finding the interactive toolbar at the bottom
            toolbar_locator = page.locator(".toolbar_main_3Mxwo")

            # Helper to extract digits from a text segment
            def _first_number_or_zero(s: str) -> str:
                m = re.search(r'(\d+)', s or "")
                return m.group(1) if m else "0"

            share_count = "0"
            comment_count = "0"
            like_count = "0"

            try:
                toolbar_text = ""
                if await toolbar_locator.count() > 0:
                    toolbar_text = await toolbar_locator.first.inner_text()
                    parts = [p.strip() for p in toolbar_text.splitlines() if p.strip()]
                    # If we got at least 3 parts, take them; otherwise try to find numbers by regex
                    if len(parts) >= 3:
                        share_count = _first_number_or_zero(parts[0])
                        comment_count = _first_number_or_zero(parts[1])
                        like_count = _first_number_or_zero(parts[2])
                    else:
                        # fallback: try to extract first three numbers from the whole toolbar text
                        nums = re.findall(r'(\d+)', toolbar_text)
                        if len(nums) >= 3:
                            share_count, comment_count, like_count = nums[0], nums[1], nums[2]
                        elif len(nums) == 2:
                            share_count, comment_count = nums[0], nums[1]
                        elif len(nums) == 1:
                            comment_count = nums[0]
                        # any missing remain "0"
                else:
                    logger.info("⚠️ Toolbar not found, defaulting counts to 0")
            except Exception as e:
                logger.warning(f"Failed to parse toolbar counts: {e}")

            # comments scraping
            comments = await scrape_comments(page, url, max_comments=20)
            comments = json.dumps(comments, ensure_ascii=False) if comments else None
            
            item = [{
                'unnamed': None,
                'user_name': title,
                'publication_date': publish_date,
                'content': content,
                'shared_count': share_count if share_count else '0',
                'comment_count': comment_count if comment_count else '0',
                'like_count': like_count if like_count else '0',
                'link1': url,
                'link2': None,
                'content_segmented': None,
                'is_agriculture_related': None,
                'index_number': None,
                'comments': comments
            }]
            logger.info(f"💾 Inserting scraped data into the database...: {item}")

            utils.insert_data(conn, config.table_name, item)

        except Exception as e:
            logger.info(f"❌ An error occurred: {e}")
            logger.info("Possible reasons: The page structure has changed, or content failed to load due to unsuccessful login.")
        finally:
            logger.info("🗑️ Closing browser.")
            await browser.close()


async def scrape_comments(page, url, max_comments: int = 20):
    """
    Scrape comments for a weibo post url and save each comment as:
    {
      'username': "用户A",
      'content': "test",
      'time': "2025-11-26",
      'likes': '10'
    }
    NOTE: selectors are best-effort and may need adjustment if weibo HTML changes.
    """
    logger.info(f"🔎 Scraping comments for: {url}")
    comments: list[dict] = []

    try:
        await page.wait_for_timeout(1000)
        # await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

        # Scroll to load more comments
        logger.info("📜 Scrolling to load comments...")
        for i in range(3):
            try:
                await page.evaluate("window.scrollBy(0, 500)")
                await page.wait_for_timeout(2000)
                logger.info(f"Scroll {i+1}/3 completed")
            except Exception as e:
                logger.error(f"Scroll error: {e}")
        
        comments_area_div = page.locator('div[class^="RepostCommentList_mar1_"]')
        comments_div = comments_area_div.locator('div[class="con1 woo-box-item-flex"]')
        
        # Get count of comment elements
        count = await comments_div.count()
        if count == 0:
            logger.info(f"No comments found for {url}")
            return comments
        comments_text = ""
        limit = min(count, max_comments)
        
        # Iterate through each comment element using nth()
        for i in range(limit):
            try:
                comment_element = comments_div.nth(i)
                comment_inner_text = await comment_element.inner_text()
                comment_inner_text = comment_inner_text.replace('\n', ' ').strip()
                comments_text += comment_inner_text + "\n"
            except Exception as e:
                logger.warning(f"Failed to extract comment {i}: {e}")
                continue
        
        # Extract structured comment data from text
        comments = utils.extract_weibo_comments_from_text(comments_text)
        logger.info(f"✅ Successfully scraped {len(comments)} comments from {url}")
        
    except Exception as e:
        logger.info(f"❌ An error occurred while scraping comments: {e}")

    return comments
