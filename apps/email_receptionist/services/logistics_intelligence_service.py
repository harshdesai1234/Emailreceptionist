"""
LogisticsIntelligenceService — Dynamic lane rate card lookup, live shipment tracking,
and vehicle placement / driver details retrieval.
"""
import logging
import re
from typing import Any, Dict, Optional, Tuple
from email_receptionist.models import LogisticsLaneRate, LogisticsShipment

logger = logging.getLogger(__name__)


class LogisticsIntelligenceService:

    @classmethod
    def lookup_lane_rate(cls, origin: str, destination: str, tenant=None, vehicle_type: str = '') -> Optional[Dict[str, Any]]:
        """
        Finds a matching lane rate card for an origin and destination.
        Matches case-insensitively or via substring (e.g. 'mumbai' in 'navi mumbai').
        """
        if not origin or not destination:
            return None

        origin = origin.strip().lower()
        destination = destination.strip().lower()

        qs = LogisticsLaneRate.objects.filter(is_active=True)
        if tenant:
            qs = qs.filter(tenant=tenant)

        # 1. Exact match
        exact = qs.filter(origin_city__iexact=origin, destination_city__iexact=destination).first()
        if exact:
            return cls._serialize_lane(exact)

        # 2. Substring match (e.g. "Hubli" / "Hubballi", "Bengaluru" / "Bangalore")
        for lane in qs:
            l_orig = lane.origin_city.lower()
            l_dest = lane.destination_city.lower()
            if (l_orig in origin or origin in l_orig) and (l_dest in destination or destination in l_dest):
                return cls._serialize_lane(lane)

        return None

    @classmethod
    def lookup_shipment_by_lr(cls, lr_query: str, tenant=None) -> Optional[Dict[str, Any]]:
        """
        Finds a shipment by LR / AWB number or extracted string.
        """
        if not lr_query:
            return None

        cleaned = re.sub(r'[^A-Za-z0-9\-_]', '', str(lr_query)).strip()
        if not cleaned:
            return None

        qs = LogisticsShipment.objects.all()
        if tenant:
            qs = qs.filter(tenant=tenant)

        # Match exact or clean
        shipment = qs.filter(lr_number__iexact=cleaned).first()
        if not shipment:
            shipment = qs.filter(lr_number__icontains=cleaned).first()

        if shipment:
            return cls._serialize_shipment(shipment)
        return None

    @classmethod
    def lookup_shipment_by_context(cls, text: str, sender_email: str = '', tenant=None) -> Optional[Dict[str, Any]]:
        """
        Attempts to find a shipment referenced in the email text or sender email.
        """
        if not text:
            return None

        # Look for LR patterns: LR-1234, AWB#1234, #12345, LR: 9921
        patterns = [
            r'(?:LR|AWB|B/?L|CONS|TRACKING|SHIPMENT)[\s#:\.\-]*([A-Za-z0-9\-]{4,20})',
            r'#([A-Za-z0-9\-]{5,20})'
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                lr = match.group(1).strip()
                res = cls.lookup_shipment_by_lr(lr, tenant=tenant)
                if res:
                    return res

        # Try matching by customer email if open shipment exists
        if sender_email:
            qs = LogisticsShipment.objects.filter(customer_email__iexact=sender_email).exclude(status__in=['closed'])
            if tenant:
                qs = qs.filter(tenant=tenant)
            recent = qs.order_by('-created_at').first()
            if recent:
                return cls._serialize_shipment(recent)

        return None

    @classmethod
    def build_logistics_context_for_prompt(cls, inbound_message: str, sender_email: str = '', tenant=None) -> str:
        """
        Synthesizes relevant logistics lane rates and active shipments into a context block
        for the AI draft prompt.
        """
        lines = []

        # 1. Check for shipment tracking or vehicle placement inquiry
        shipment = cls.lookup_shipment_by_context(inbound_message, sender_email=sender_email, tenant=tenant)
        if shipment:
            lines.append("=== LOGISTICS SHIPMENT & VEHICLE DISPATCH DATA FOUND IN DATABASE ===")
            lines.append(f"LR / Consignment Number: {shipment['lr_number']}")
            lines.append(f"Shipment Status: {shipment['status_display']}")
            lines.append(f"Route: {shipment['origin_city']} to {shipment['destination_city']}")
            lines.append(f"Cargo: {shipment['cargo_type']} ({shipment['weight_or_volume']})")
            lines.append(f"Current Live Checkpoint / Location: {shipment['current_location']}")
            lines.append(f"Expected Delivery Schedule: {shipment['expected_delivery'] or 'In Transit'}")
            if shipment.get('truck_number'):
                lines.append(f"Allocated Vehicle: {shipment['truck_number']} ({shipment['vehicle_type']})")
                lines.append(f"Driver Name: {shipment['driver_name']}")
                lines.append(f"Driver Mobile Contact: {shipment['driver_phone']}")
                lines.append(f"Reporting / Placement Time: {shipment['placement_reporting_time'] or 'As per schedule'}")
                if shipment.get('eway_bill_number'):
                    lines.append(f"E-Way Bill: {shipment['eway_bill_number']}")
            if shipment.get('remarks'):
                lines.append(f"Internal Dispatch Remarks: {shipment['remarks']}")
            lines.append("INSTRUCTION FOR AI: When replying, directly give the customer this exact live tracking/driver/vehicle detail accurately and professionally.")
            lines.append("=== END SHIPMENT DATA ===\n")

        # 2. Check for Lane Rate Card matching
        # Quick extract potential origin/destination words
        words = [w.strip(" ,.:;?!()[]") for w in inbound_message.split() if len(w) > 3]
        matched_lane = None
        for i, w1 in enumerate(words):
            for w2 in words[i+1:]:
                lane = cls.lookup_lane_rate(w1, w2, tenant=tenant)
                if lane:
                    matched_lane = lane
                    break
            if matched_lane:
                break

        if matched_lane:
            lines.append("=== VERIFIED LANE RATE CARD FOUND IN DATABASE ===")
            lines.append(f"Lane Route: {matched_lane['origin_city']} ➔ {matched_lane['destination_city']}")
            lines.append(f"Standard Vehicle: {matched_lane['vehicle_type']}")
            lines.append(f"Base Freight Rate: ₹{matched_lane['base_rate']:,.2f} ({matched_lane['rate_type_display']})")
            lines.append(f"Transit Time: {matched_lane['transit_time_label']}")
            lines.append(f"Detention Terms: ₹{matched_lane['detention_charge_per_day']:,.2f}/day after 24 hrs loading/unloading.")
            lines.append(f"Standard Commercial Terms: {matched_lane['terms_notes']}")
            lines.append("INSTRUCTION FOR AI: Quote this exact lane rate, transit duration, and detention terms in the email draft.")
            lines.append("=== END LANE RATE CARD ===\n")

        return "\n".join(lines)

    @classmethod
    def _serialize_lane(cls, lane: LogisticsLaneRate) -> Dict[str, Any]:
        return {
            'id': lane.id,
            'origin_city': lane.origin_city,
            'destination_city': lane.destination_city,
            'vehicle_type': lane.vehicle_type,
            'rate_type': lane.rate_type,
            'rate_type_display': lane.get_rate_type_display(),
            'base_rate': float(lane.base_rate),
            'transit_days': lane.transit_days,
            'transit_time_label': lane.transit_time_label,
            'detention_charge_per_day': float(lane.detention_charge_per_day),
            'terms_notes': lane.terms_notes,
        }

    @classmethod
    def _serialize_shipment(cls, s: LogisticsShipment) -> Dict[str, Any]:
        return {
            'id': s.id,
            'lr_number': s.lr_number,
            'customer_name': s.customer_name,
            'customer_email': s.customer_email,
            'origin_city': s.origin_city,
            'destination_city': s.destination_city,
            'cargo_type': s.cargo_type,
            'weight_or_volume': s.weight_or_volume,
            'status': s.status,
            'status_display': s.get_status_display(),
            'current_location': s.current_location,
            'expected_delivery': s.expected_delivery,
            'truck_number': s.truck_number,
            'vehicle_type': s.vehicle_type,
            'driver_name': s.driver_name,
            'driver_phone': s.driver_phone,
            'eway_bill_number': s.eway_bill_number,
            'placement_reporting_time': s.placement_reporting_time,
            'remarks': s.remarks,
        }

