import puppeteer from 'puppeteer';

(async () => {
    // wait for vite to be ready
    await new Promise(resolve => setTimeout(resolve, 2000));
    const browser = await puppeteer.launch({ args: ['--no-sandbox'] });
    const page = await browser.newPage();
    page.on('console', msg => {
        if (msg.type() === 'error') {
            console.log('BROWSER_ERROR:', msg.text());
        }
    });
    page.on('pageerror', err => {
        console.log('PAGE_ERROR:', err.toString());
    });
    
    await page.goto('http://localhost:6001/devices', { waitUntil: 'networkidle2' });
    await new Promise(resolve => setTimeout(resolve, 5000));
    await browser.close();
})();
