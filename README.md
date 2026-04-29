# Kupi - The Open Coupon Engine ⚡🚀

Kupi is an open-source, real-time coupon scraping and validation engine. It automatically fetches, verifies, and ranks the best discount codes for platforms like Blinkit, Zepto, Swiggy, Zomato, Amazon, and Flipkart based on your cart value.

## Features
- **🌐 Real-Time Fetching**: Scrapes live coupon sites (GrabOn, CouponDunia, etc.) on demand.
- **🧠 Smart Ranking**: Calculates actual savings based on your exact cart value, minimum order rules, and maximum discount caps to highlight the true *Best Deal*.
- **📊 Live Usage Metrics**: Simulates real-time usage metrics and visual feedback to create an engaging, dynamic user experience.
- **🗂️ Local Database**: Save verified coupons to your local browser storage to build your own personal coupon database.
- **🚀 Dual Mode**: Can be run as a standalone HTML file (`kupi.html`) for saved coupons, or backed by the Python Flask server (`kupi_server.py`) for live fetching.

## Installation & Usage

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/kupi.git
   cd kupi
   ```

2. **Install the Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the server:**
   ```bash
   python kupi_server.py
   ```

4. **Open the app:**
   Navigate to [http://localhost:5000](http://localhost:5000) in your web browser.

## Tech Stack
- **Backend**: Python, Flask, BeautifulSoup4, Requests (for web scraping).
- **Frontend**: Vanilla HTML/CSS/JS with a custom-built, modern glassmorphism UI.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
