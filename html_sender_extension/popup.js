async function sendHTML() {
    const status = document.getElementById("status");
    status.textContent = "Sending...";

    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => document.documentElement.outerHTML
    }, async (results) => {
        const html = results[0].result;

        try {
            const res = await fetch("http://127.0.0.1:5000/htmls", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ html })
            });

            status.textContent = "Sent successfully ✓";
        } catch (e) {
            status.textContent = "Error sending ✗";
        }
    });
}

// Run immediately when popup opens
document.addEventListener("DOMContentLoaded", sendHTML);