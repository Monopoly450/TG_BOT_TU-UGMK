"""Offline launch checks. Native Telegram behavior still needs device testing."""
import asyncio
import json
from pathlib import Path
from jinja2 import Environment
from playwright.async_api import async_playwright


async def main():
    template = Environment(autoescape=True).from_string(Path('templates/webapp_launch.html').read_text())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        cases = [
            ('macos', False, True, False), ('tdesktop', False, True, False),
            ('ios', False, True, False), ('android', False, True, False),
            ('android_x', False, True, False), ('macos', True, True, False),
            ('macos', True, False, False), ('macos', False, False, False),
            ('macos', False, True, True), ('unknown', False, True, False),
        ]
        for platform, signed, enabled, failing in cases:
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route_handler(route):
                url = route.request.url
                requests.append(url)
                if url.startswith('https://telegram.org/'):
                    config = json.dumps({'platform': platform, 'signed': signed, 'failing': failing})
                    await route.fulfill(content_type='text/javascript', body="""
                        const config = """ + config + """;
                        window.nativeEvents=[]; window.failOpen=config.failing;
                        window.Telegram={WebApp:{ready(){}, platform:config.platform,
                            initData:config.signed?'signed-data':'',
                            isVersionAtLeast(){return config.platform !== 'unknown'},
                            openTelegramLink(url){
                                if(window.failOpen)throw Error('Host unavailable');
                                if(config.platform.startsWith('android') && !navigator.userActivation.isActive)return;
                                nativeEvents.push(['open',url]);
                            },
                            close(){nativeEvents.push(['close'])},
                            sendData(){throw Error('Must not send messages')}
                        }};
                    """)
                elif url == 'https://t.me/campus_test_bot?startapp=keyboard' and platform.startswith('android'):
                    # Android's shouldOverrideUrlLoading handles the URI in the
                    # native client and keeps the source document alive.
                    await route.fulfill(status=204)
                elif 'launch=keyboard' in url:
                    await route.fulfill(content_type='text/html', body=template.render(
                        launch_url='https://t.me/campus_test_bot?startapp=keyboard' if enabled else None,
                        launch_message='Открываем приложение…' if enabled else 'Прямой вход пока настраивается.'))
                elif url.startswith('https://miniapp.test/webapp'):
                    await route.fulfill(content_type='text/html', body='<p id="app">Signed app entry</p>')
                else:
                    await route.abort()
            await page.route('**/*', route_handler)
            await page.goto('https://miniapp.test/webapp?launch=keyboard#tgWebAppData=signed-data', wait_until='commit')
            if signed:
                await page.wait_for_selector('#app')
                assert page.url == 'https://miniapp.test/webapp#tgWebAppData=signed-data'
            else:
                await page.wait_for_function("typeof nativeEvents !== 'undefined'")
                if not enabled or platform == 'unknown':
                    await page.wait_for_timeout(250)
                    assert await page.evaluate('nativeEvents') == []
                    assert await page.locator('#launch-button').is_enabled()
                else:
                    if failing:
                        await page.wait_for_timeout(250)
                        assert await page.evaluate('nativeEvents') == []
                        await page.evaluate('window.failOpen = false')
                        await page.locator('#launch-button').click()
                    # Even programmatic duplicate calls cannot dispatch twice.
                    await page.evaluate('launch();launch()')
                    await page.wait_for_function("nativeEvents.some(event => event[0] === 'close')")
                    expected = [['close']] if platform.startswith('android') else [
                        ['open', 'https://t.me/campus_test_bot?startapp=keyboard'], ['close']]
                    assert await page.evaluate('nativeEvents') == expected
                    assert await page.locator('#launch-button').is_disabled()
            assert not any('/api/' in url for url in requests), requests
            telegram_navigations = [url for url in requests if url.startswith('https://t.me/')]
            expected_navigations = ['https://t.me/campus_test_bot?startapp=keyboard'] if platform.startswith('android') and enabled and not signed else []
            assert telegram_navigations == expected_navigations, requests
            assert not errors, errors
            await page.close()
        await browser.close()
    print('Launch checks passed: Android automatic intercepted navigation without tap, desktop SDK routing, source close, duplicate guard, signed entry and retry.')


asyncio.run(main())
