from flask import Flask, request, jsonify
from flask_cors import CORS
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os.path
import json
from datetime import datetime, timedelta
import requests
from typing import Dict, List, Optional
import logging

# If modifying these scopes, delete the file token.pickle.
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Distance Matrix API key
DISTANCE_MATRIX_API_KEY = 'AIzaSyBAbKwlYvJxAEJYZ5FZd6S7or2bJ6Wqjtc'

# Shop location (Montague Pianos)
SHOP_LOCATION = "53 High Street, Northchurch, Herts, HP4 3QH"

# Calendar ID for the piano tuning calendar
CALENDAR_ID = 'clivestunings@googlemail.com'  # Clive's tuning calendar

# Available time slots
AVAILABLE_SLOTS = {
    'Tuesday': ['10:30', '12:00', '13:30', '15:00', '16:00'],
    'Wednesday': ['09:00', '10:30', '12:00', '13:30', '15:00', '16:00'],
    'Thursday': ['10:30', '12:00', '13:30', '15:00']
}

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Store pending bookings (in a real app, use a database)
pending_bookings = {}

@app.before_request
def log_request_info():
    """Log all incoming requests."""
    print(f"\n{'='*50}")
    print(f"Incoming {request.method} request to {request.path}")
    print(f"Headers: {dict(request.headers)}")
    print(f"Content-Type: {request.content_type}")
    
    try:
        if request.is_json:
            print(f"JSON Body: {json.dumps(request.get_json(), indent=2)}")
        elif request.form:
            print(f"Form data: {dict(request.form)}")
        elif request.data:
            print(f"Raw data: {request.data.decode('utf-8')}")
    except Exception as e:
        print(f"Error reading request data: {str(e)}")
    print(f"{'='*50}\n")

@app.after_request
def log_response_info(response):
    """Log all outgoing responses."""
    print(f"\n{'='*50}")
    print(f"Outgoing response to {request.path}")
    print(f"Status: {response.status}")
    print(f"Headers: {dict(response.headers)}")
    print(f"{'='*50}\n")
    return response

def get_google_calendar_service():
    """Get Google Calendar service using service account."""
    try:
        # Try Render environment path first
        credentials_path = '/etc/secrets/service-account-key.json'
        if not os.path.exists(credentials_path):
            # Fall back to local development path
            credentials_path = 'credentials/service-account-key.json'
            
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Could not find credentials file at {credentials_path}")
            
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES
        )
        return build('calendar', 'v3', credentials=credentials)
    except Exception as e:
        print(f"Error initializing calendar service: {e}")
        raise

def check_distance(origin: str) -> Optional[int]:
    """Check distance from origin to shop using Distance Matrix API."""
    try:
        url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={origin}&destinations={SHOP_LOCATION}&key={DISTANCE_MATRIX_API_KEY}"
        response = requests.get(url)
        data = response.json()
        
        if data['status'] == 'OK' and data['rows'][0]['elements'][0]['status'] == 'OK':
            # Use distance instead of duration
            return data['rows'][0]['elements'][0]['distance']['value']  # Distance in meters
        return None
    except Exception as e:
        print(f"Error checking distance: {e}")
        return None

def check_distance_from_adjacent_bookings(service, date: str, time: str, customer_address: str) -> bool:
    """Check if the booking location is within 10 miles of any adjacent bookings."""
    try:
        # Get the booking time and make it timezone aware
        booking_datetime = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M').astimezone()
        
        print(f"\nChecking distances for slot on {date} at {time}")
        print(f"Customer postcode: {customer_address}")
        print(f"\n{'='*80}")
        print(f"Checking bookings for {booking_datetime.strftime('%Y-%m-%d')} at {booking_datetime.strftime('%H:%M')}")
        print(f"{'='*80}\n")
        
        # Get events for the entire day
        day_start = booking_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = (day_start + timedelta(days=1))
        
        print(f"Searching for events between {day_start.strftime('%Y-%m-%d %H:%M')} and {day_end.strftime('%Y-%m-%d %H:%M')}")
        
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy='startTime',
            maxResults=100
        ).execute()
        
        events = events_result.get('items', [])
        
        # Debug: Print raw events
        print(f"\nFound {len(events)} events for this day:")
        for event in events:
            print(f"Event: {event.get('summary', '')} at {event.get('start', {}).get('dateTime', '')}")
            if 'description' in event:
                print(f"Description: {event.get('description', '')}")
        
        # Check if the day is blocked out with NO CLIVE
        for event in events:
            summary = event.get('summary', '').lower()
            # Check for NO CLIVE blocks that span the whole day
            if 'no clive' in summary:
                event_start = event.get('start', {}).get('dateTime')
                event_end = event.get('end', {}).get('dateTime')
                if event_start and event_end:
                    start_time = datetime.fromisoformat(event_start.replace('Z', '+00:00')).astimezone()
                    end_time = datetime.fromisoformat(event_end.replace('Z', '+00:00')).astimezone()
                    # If the block covers the entire tuning day (9am to 5pm)
                    if (start_time.hour <= 9 and end_time.hour >= 17):
                        print(f"\nX Day REJECTED: {date} is blocked out (NO CLIVE all day)")
                        return False
        
        # Convert all events to a list of (datetime, address) tuples
        event_details = []
        for event in events:
            try:
                summary = event.get('summary', '').lower()
                description = event.get('description', '').lower()
                event_start = event['start'].get('dateTime')
                
                # Skip only non-booking events
                if '@' in summary or summary.startswith('available'):
                    print(f"Skipping non-booking event: {event.get('summary')}")
                    continue
                
                # Handle time slot lists (e.g., "09:00 10:30 12:00 13:30 15:00 16:00")
                if all(slot.replace(':', '').isdigit() for slot in summary.split()):
                    # This is a time slot list, create individual events for each time
                    time_slots = summary.split()
                    for slot in time_slots:
                        slot_datetime = datetime.strptime(f"{date} {slot}", '%Y-%m-%d %H:%M').astimezone()
                        event_details.append((slot_datetime, None))  # No address for time slot lists
                    continue
                
                # Get the location from either the description or the summary
                event_address = None
                
                # First try to get a structured address from the description
                if description:
                    if 'Address:' in description:
                        event_address = description.split('Address:')[1].split('\n')[0].strip()
                    else:
                        # Try to find a location in the description
                        desc_lines = description.split('\n')
                        if len(desc_lines) > 0:
                            event_address = desc_lines[0].strip()
                
                # If no address found in description, try to get location from the summary
                if not event_address:
                    # Remove common prefixes/suffixes that aren't part of the location
                    location_text = summary
                    for prefix in ['no clive', 'available']:
                        location_text = location_text.replace(prefix, '').strip()
                    
                    # Split remaining text and look for location words
                    words = location_text.split()
                    location_words = [w for w in words if len(w) > 3 and w not in ['new', 'customer', 'please']]
                    if location_words:
                        event_address = location_words[-1].title()
                
                if event_start:
                    event_datetime = datetime.fromisoformat(event_start.replace('Z', '+00:00')).astimezone()
                    event_details.append((event_datetime, event_address))
                    print(f"Added booking: {event_datetime.strftime('%H:%M')} at {event_address}")
            except Exception as e:
                print(f"Error processing event: {e}")
                continue
        
        # Sort events by time
        event_details.sort(key=lambda x: x[0])
        
        print(f"\nProcessed {len(event_details)} valid bookings for this day:")
        for time, address in event_details:
            print(f"- {time.strftime('%H:%M')}: {address}")
        
        # Find the position where our new booking would fit
        insert_index = 0
        for i, (event_time, _) in enumerate(event_details):
            if event_time > booking_datetime:
                break
            insert_index = i + 1
        
        print(f"\nProposed booking at {booking_datetime.strftime('%H:%M')} would be booking #{insert_index + 1} of {len(event_details) + 1}")
        
        # Check distances from adjacent bookings
        distances = []
        
        # Check previous booking if it exists
        if insert_index > 0:
            prev_time, prev_address = event_details[insert_index - 1]
            if prev_address:  # Only check distance if we have a valid address
                url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={customer_address}&destinations={prev_address}&key={DISTANCE_MATRIX_API_KEY}"
                response = requests.get(url)
                data = response.json()
                
                if data['status'] == 'OK' and data['rows'][0]['elements'][0]['status'] == 'OK':
                    distance = data['rows'][0]['elements'][0]['distance']['value']  # Distance in meters
                    prev_distance = distance / 1609.34  # Convert to miles
                    time_diff = (booking_datetime - prev_time).total_seconds() / 3600  # Convert to hours
                    distances.append(('previous', prev_distance, time_diff, prev_address))
        
        # Check next booking if it exists
        if insert_index < len(event_details):
            next_time, next_address = event_details[insert_index]
            if next_address:  # Only check distance if we have a valid address
                url = f"https://maps.googleapis.com/maps/api/distancematrix/json?origins={customer_address}&destinations={next_address}&key={DISTANCE_MATRIX_API_KEY}"
                response = requests.get(url)
                data = response.json()
                
                if data['status'] == 'OK' and data['rows'][0]['elements'][0]['status'] == 'OK':
                    distance = data['rows'][0]['elements'][0]['distance']['value']  # Distance in meters
                    next_distance = distance / 1609.34  # Convert to miles
                    time_diff = (next_time - booking_datetime).total_seconds() / 3600  # Convert to hours
                    distances.append(('next', next_distance, time_diff, next_address))
        
        # Print distances in a clear format
        print("\nDistance Summary:")
        print("-" * 80)
        if not distances:
            print("No adjacent bookings found")
        for booking_type, distance, time_diff, address in distances:
            print(f"Distance to {booking_type} booking ({address}): {distance:.1f} miles ({time_diff:.1f} hours {booking_type})")
        print("-" * 80)
        
        # Check if any distance is too far
        for booking_type, distance, time_diff, address in distances:
            if distance > 10:
                print(f"\nX Slot REJECTED: {date} at {time}")
                print(f"X {booking_type.capitalize()} booking at {address} is {distance:.1f} miles away (more than 10 miles)")
                return False
        
        if not distances:
            print("\n✓ No adjacent bookings found - slot is valid")
        else:
            print("\n✓ All distance checks passed - slot is valid")
        
        print(f"✓ Slot ACCEPTED: {date} at {time}")
        return True
        
    except Exception as e:
        print(f"Error checking adjacent bookings: {e}")
        print(f"X Slot REJECTED: {date} at {time}")
        return False

def get_available_slots(date: str) -> List[str]:
    """Get available time slots for a given date."""
    try:
        day = datetime.strptime(date, '%Y-%m-%d').strftime('%A')
        return AVAILABLE_SLOTS.get(day, [])
    except ValueError:
        return []

def check_slot_availability(service, date: str, time: str) -> bool:
    """Check if a specific time slot is available in Google Calendar."""
    try:
        # Ensure date is in YYYY-MM-DD format and make it timezone-aware
        try:
            # If date is already in YYYY-MM-DD format, use it directly
            slot_datetime = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M %p').astimezone()
        except ValueError:
            try:
                # Try with just YYYY-MM-DD and HH:MM format
                slot_datetime = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M').astimezone()
            except ValueError:
                # If date is in another format, try to parse it
                try:
                    # Try parsing with different formats
                    for fmt in ['%A, %B %d', '%B %d', '%d/%m/%Y', '%Y-%m-%d']:
                        try:
                            slot_datetime = datetime.strptime(f"{date} {time}", f"{fmt} %H:%M").astimezone()
                            break
                        except ValueError:
                            continue
                    else:
                        raise ValueError(f"Could not parse date: {date}")
                except ValueError as e:
                    print(f"Error parsing date: {e}")
                    return False
        
        # Format the time with timezone
        time_min = slot_datetime.isoformat()
        time_max = (slot_datetime + timedelta(hours=1)).isoformat()
        
        print(f"Checking availability for slot: {time_min} to {time_max}")
        
        # Get events for the entire day to check for time slot lists
        day_start = slot_datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = (day_start + timedelta(days=1))
        
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # Check for time slot lists and existing bookings
        for event in events:
            summary = event.get('summary', '').lower()
            
            # Check if this is a time slot list
            if all(slot.replace(':', '').isdigit() for slot in summary.split()):
                time_slots = summary.split()
                if time in time_slots:
                    print(f"Found time slot list containing {time}")
                    return False
            
            # Check event duration and overlap
            event_start = event.get('start', {}).get('dateTime')
            event_end = event.get('end', {}).get('dateTime')
            
            if event_start and event_end:
                start_time = datetime.fromisoformat(event_start.replace('Z', '+00:00')).astimezone()
                end_time = datetime.fromisoformat(event_end.replace('Z', '+00:00')).astimezone()
                
                # Check if the event overlaps with our slot
                if (start_time <= slot_datetime and end_time > slot_datetime) or \
                   (start_time < (slot_datetime + timedelta(hours=1)) and end_time >= (slot_datetime + timedelta(hours=1))):
                    print(f"Found overlapping event: {event.get('summary', '')} from {start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')}")
                    return False
        
        return True
    except Exception as e:
        print(f"Error checking slot availability: {e}")
        return False

def check_day_availability(service, date_str: str) -> bool:
    """Check if the entire day is available (no 8-hour bookings between 09:00-17:00)."""
    try:
        # Parse the date and make it timezone-aware
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').astimezone()
        
        # Set time limits for the day (9am to 5pm)
        day_start = date_obj.replace(hour=9, minute=0, second=0, microsecond=0)
        day_end = date_obj.replace(hour=17, minute=0, second=0, microsecond=0)
        
        # Format the times with timezone
        time_min = day_start.isoformat()
        time_max = day_end.isoformat()
        
        print(f"Checking day availability for: {date_str} between 09:00-17:00")
        
        # Check events in the calendar
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        events = events_result.get('items', [])
        
        # Check for time slot lists first
        for event in events:
            summary = event.get('summary', '').lower()
            if all(slot.replace(':', '').isdigit() for slot in summary.split()):
                print(f"Found time slot list for the day: {summary}")
                return False
        
        # Look for a booking that spans the entire 09:00-17:00 period (8 hours)
        for event in events:
            try:
                event_start = event.get('start', {}).get('dateTime')
                event_end = event.get('end', {}).get('dateTime')
                
                if event_start and event_end:
                    start_time = datetime.fromisoformat(event_start.replace('Z', '+00:00')).astimezone()
                    end_time = datetime.fromisoformat(event_end.replace('Z', '+00:00')).astimezone()
                    
                    duration = end_time - start_time
                    duration_hours = duration.total_seconds() / 3600
                    
                    # If there's an event that spans close to the full day (7+ hours)
                    # or covers the entire 9am-5pm period
                    if (duration_hours >= 7 or 
                        (start_time <= day_start and end_time >= day_end)):
                        print(f"Found a full-day booking on {date_str}:")
                        print(f"- {event.get('summary', '')} from {start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')} ({duration_hours:.1f} hours)")
                        return False
                    
                    print(f"Event on {date_str}: {event.get('summary', '')} from {start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')} ({duration_hours:.1f} hours) - not a full day booking")
            except Exception as e:
                print(f"Error processing event timing: {e}")
                continue
        
        print(f"No full-day booking found on {date_str}")
        return True
    except Exception as e:
        print(f"Error checking day availability: {e}")
        return False

def create_booking(service, date: str, time: str, customer_name: str, address: str, phone: str) -> bool:
    """Create a booking in Google Calendar."""
    try:
        print(f"Attempting to create booking for {customer_name} on {date} at {time}")
        slot_datetime = datetime.strptime(f"{date} {time}", '%Y-%m-%d %H:%M')
        
        # Extract area from address
        # Split by comma and take the second part (after the street address)
        address_parts = [part.strip() for part in address.split(',')]
        area = address_parts[1] if len(address_parts) > 1 else ""
        
        event = {
            'summary': f'{customer_name} {area}',
            'description': f'Customer: {customer_name}\nAddress: {address}\nPhone: {phone}',
            'start': {
                'dateTime': slot_datetime.astimezone().isoformat(),
                'timeZone': 'Europe/London',
            },
            'end': {
                'dateTime': (slot_datetime + timedelta(hours=1)).astimezone().isoformat(),
                'timeZone': 'Europe/London',
            },
        }
        
        print(f"Creating event with details: {json.dumps(event, indent=2)}")
        created_event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        print(f"Successfully created event: {created_event.get('id')}")
        return True
    except Exception as e:
        print(f"Error creating booking: {str(e)}")
        return False

@app.route('/check-availability', methods=['POST'])
def check_availability():
    """Check available slots for the next month."""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400
            
        data = request.get_json()
        postcode = data.get('postcode')
        last_offered_date = data.get('last_offered_date')  # Get the last date we offered slots for
        
        if not postcode:
            return jsonify({'error': 'Postcode is required'}), 400
            
        # Check distance from shop
        distance = check_distance(postcode)
        if distance is None:
            return jsonify({'error': 'Could not verify distance'}), 400
            
        # Convert to miles (roughly 1 mile = 1609 meters)
        distance_miles = distance / 1609
        print(f"\nDistance from shop: {distance_miles:.1f} miles")
        
        if distance_miles > 20:
            return jsonify({
                'error': 'Location too far',
                'message': 'Your location is more than 20 miles from our shop. Please call Lee on 01442 876131 to discuss your booking.'
            }), 400
        
        service = get_google_calendar_service()
        current_date = datetime.now()
        end_date = current_date + timedelta(days=28)  # Look ahead 4 weeks
        
        # If last_offered_date is provided, start from the day after
        if last_offered_date:
            try:
                last_date = datetime.strptime(last_offered_date, '%Y-%m-%d')
                current_date = last_date + timedelta(days=1)
            except ValueError:
                print(f"Error parsing last_offered_date: {last_offered_date}")
        
        # First, get all available slots
        all_available_slots = []
        current_check_date = current_date
        
        print("\nFinding all available slots...")
        while current_check_date <= end_date:
            # Only check Tuesdays, Wednesdays, and Thursdays
            if current_check_date.weekday() in [1, 2, 3]:  # 1=Tuesday, 2=Wednesday, 3=Thursday
                date_str = current_check_date.strftime('%Y-%m-%d')
                
                # Check if the day has any bookings between 09:00-17:00
                if check_day_availability(service, date_str):
                    available_slots = get_available_slots(date_str)
                    
                    if available_slots:
                        # Check each slot's availability
                        for time in available_slots:
                            if check_slot_availability(service, date_str, time):
                                all_available_slots.append({
                                    'date': date_str,
                                    'time': time
                                })
                else:
                    print(f"Day {date_str} has existing bookings between 09:00-17:00, skipping all slots")
            
            current_check_date += timedelta(days=1)
        
        print(f"\nFound {len(all_available_slots)} available slots")
        
        # Now check distances from adjacent bookings for each slot
        valid_slots = []
        for slot in all_available_slots:
            print(f"\nChecking distances for slot on {slot['date']} at {slot['time']}")
            if check_distance_from_adjacent_bookings(service, slot['date'], slot['time'], postcode):
                valid_slots.append(slot)
                print(f"✓ Slot accepted: {slot['date']} at {slot['time']}")
            else:
                print(f"❌ Slot rejected: {slot['date']} at {slot['time']}")
        
        if not valid_slots:
            return jsonify({
                'error': 'No suitable slots found',
                'message': 'No suitable tuning slots found within 10 miles of adjacent bookings. Please call Lee on 01442 876131 to discuss your booking.'
            }), 400
        
        # Group slots by date for better presentation
        slots_by_date = {}
        for slot in valid_slots:
            date = slot['date']
            if date not in slots_by_date:
                slots_by_date[date] = []
            slots_by_date[date].append(slot['time'])
        
        # Format the response message
        message = "We have available piano tuning slots on the following dates:\n"
        for date, times in slots_by_date.items():
            date_obj = datetime.strptime(date, '%Y-%m-%d')
            message += f"- {date_obj.strftime('%A, %B %d')} at {', '.join(times)}\n"
        
        message += "\nPlease let me know which slot you'd prefer, and I'll book it for you. I'll need your name, address, and phone number to complete the booking."
        
        # Return all valid slots, not just the first 5
        return jsonify({
            'available_slots': valid_slots,
            'total_slots': len(valid_slots),
            'message': message
        })
    except Exception as e:
        print(f"Error in check_availability: {e}")
        return jsonify({'error': 'Failed to check availability'}), 500

@app.route('/create-booking', methods=['POST'])
def create_booking_endpoint():
    """Create a new booking."""
    try:
        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        data = request.get_json()
        required_fields = ['date', 'time', 'customer_name', 'address', 'phone']
        
        if not all(field in data for field in required_fields):
            missing_fields = [field for field in required_fields if field not in data]
            return jsonify({
                'error': 'Missing required fields',
                'missing_fields': missing_fields
            }), 400
        
        service = get_google_calendar_service()
        if not check_slot_availability(service, data['date'], data['time']):
            return jsonify({'error': 'Selected time slot is no longer available'}), 400

        distance = check_distance(data['address'])
        if distance is None:
            return jsonify({'error': 'Could not verify distance'}), 400

        # Create the booking
        if create_booking(service, data['date'], data['time'], 
                         data['customer_name'], data['address'], data['phone']):
            # Return success message without payment info
            return jsonify({
                'message': f'Great! Your piano tuning appointment is all set for {data["date"]} at {data["time"]} with our piano tuner. He\'ll be visiting you at {data["address"]}.',
                'booking_details': {
                    'date': data['date'],
                    'time': data['time'],
                    'customer_name': data['customer_name'],
                    'address': data['address'],
                    'phone': data['phone'],
                    'distance': distance
                }
            })
        else:
            return jsonify({'error': 'Failed to create booking'}), 500
    except Exception as e:
        return jsonify({'error': f'Failed to create booking: {str(e)}'}), 500

@app.route('/direct-booking', methods=['POST'])
def direct_booking():
    """Create a booking directly."""
    try:
        if not request.is_json:
            print("Request is not JSON")
            return jsonify({'error': 'Content-Type must be application/json'}), 400
            
        data = request.get_json()
        print(f"Received direct booking request with data: {json.dumps(data, indent=2)}")
        
        required_fields = ['date', 'time', 'customer_name', 'address', 'phone']
        
        # Validate required fields
        if not all(field in data for field in required_fields):
            missing_fields = [field for field in required_fields if field not in data]
            print(f"Missing required fields: {missing_fields}")
            return jsonify({
                'error': 'Missing required fields',
                'missing_fields': missing_fields
            }), 400
        
        # Check if the slot is available
        service = get_google_calendar_service()
        print(f"Checking availability for {data['date']} at {data['time']}")
        if not check_slot_availability(service, data['date'], data['time']):
            print("Selected time slot is no longer available")
            return jsonify({'error': 'Selected time slot is no longer available'}), 400
        
        # Check distance
        print(f"Checking distance for address: {data['address']}")
        distance = check_distance(data['address'])
        if distance is None:
            print("Could not verify distance")
            return jsonify({'error': 'Could not verify distance'}), 400
        
        # Create the booking
        print("Attempting to create booking...")
        if create_booking(service, data['date'], data['time'], 
                         data['customer_name'], data['address'], data['phone']):
            print("Booking created successfully")
            return jsonify({
                'message': f'Your piano tuning appointment is all set for {data["date"]} at {data["time"]} with our piano tuner. He\'ll be visiting you at {data["address"]}, and the cost is £85.',
                'booking_details': {
                    'date': data['date'],
                    'time': data['time'],
                    'customer_name': data['customer_name'],
                    'address': data['address'],
                    'phone': data['phone'],
                    'distance': distance
                }
            })
        else:
            print("Failed to create booking")
            return jsonify({'error': 'Failed to create booking'}), 500
    except Exception as e:
        print(f"Error in direct_booking: {e}")
        return jsonify({'error': f'Failed to create booking: {str(e)}'}), 500

if __name__ == '__main__':
    # Run Flask with minimal configuration
    app.run(port=5002, debug=True)  # Re-enable debug mode to see more detailed errors 